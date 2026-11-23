import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.evm_crypto import BASE_USDC_ADDRESS, priv_to_address
from runtime.x402_gemini_proxy import (
    SpendLedger,
    X402GeminiClient,
    normalize_and_validate_gemini_model,
    parse_x402_payment_required,
)


class TestX402GeminiProxy(unittest.TestCase):
    def test_strict_gemini_model_enforcement(self) -> None:
        self.assertEqual(
            normalize_and_validate_gemini_model("gemini-4-hydrogen"),
            "google/gemini-4-hydrogen",
        )
        self.assertEqual(
            normalize_and_validate_gemini_model("google/gemini-3.8-flash"),
            "google/gemini-3.8-flash",
        )
        self.assertEqual(
            normalize_and_validate_gemini_model("nano-banana-pro"),
            "google/nano-banana-pro",
        )
        for bad in ("openai/gpt-4o", "anthropic/claude-sonnet-4", "deepseek/deepseek-v3", ""):
            with self.assertRaises(ValueError):
                normalize_and_validate_gemini_model(bad)

    def test_parse_x402_payment_required_verifies_base_usdc(self) -> None:
        good_doc = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:8453",
                    "asset": BASE_USDC_ADDRESS,
                    "payTo": "0xe9030014F5DAe217d0A152f02A043567b16c1aBf",
                    "amount": "2500",
                    "maxTimeoutSeconds": 300,
                }
            ],
        }
        hdr = base64.b64encode(json.dumps(good_doc).encode()).decode()
        ch, opt = parse_x402_payment_required({"PAYMENT-REQUIRED": hdr}, b"")
        self.assertEqual(opt["amount"], "2500")
        self.assertEqual(ch["x402Version"], 2)

        # Wrong token contract must be rejected
        bad_doc = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:8453",
                    "asset": "0x0000000000000000000000000000000000000001",
                    "payTo": "0xe9030014F5DAe217d0A152f02A043567b16c1aBf",
                    "amount": "2500",
                }
            ],
        }
        with self.assertRaises(ValueError):
            parse_x402_payment_required({}, json.dumps(bad_doc).encode())

    def test_spend_ledger_caps_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_p = Path(tmp) / "spend.jsonl"
            ledger = SpendLedger(max_request_micro=50_000, max_session_micro=80_000, log_path=log_p)
            ledger.reserve_or_raise(40_000)
            self.assertEqual(ledger.session_spent_micro, 40_000)
            with self.assertRaises(PermissionError):
                ledger.reserve_or_raise(60_000)  # exceeds per-request cap
            with self.assertRaises(PermissionError):
                ledger.reserve_or_raise(45_000)  # exceeds session cap
            ledger.rollback(40_000)
            self.assertEqual(ledger.session_spent_micro, 0)

    def test_forward_chat_completion_signs_x402_and_logs(self) -> None:
        test_priv = bytes.fromhex("11" * 32)
        with tempfile.TemporaryDirectory() as tmp:
            log_p = Path(tmp) / "spend.jsonl"
            ledger = SpendLedger(max_request_micro=50_000, max_session_micro=100_000, log_path=log_p)
            client = X402GeminiClient(priv_bytes=test_priv, ledger=ledger, dry_run=False)
            challenge = {
                "x402Version": 2,
                "resource": {"url": "https://blockrun.ai/api/v1/chat/completions"},
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": "eip155:8453",
                        "asset": BASE_USDC_ADDRESS,
                        "payTo": "0xe9030014F5DAe217d0A152f02A043567b16c1aBf",
                        "amount": "3200",
                        "maxTimeoutSeconds": 300,
                    }
                ],
            }
            ok_body = json.dumps(
                {"id": "chatcmpl-1", "choices": [{"message": {"role": "assistant", "content": "OK"}}]}
            ).encode()
            calls = []

            def fake_http(url, method, body=None, extra_headers=None):
                calls.append((url, extra_headers))
                if len(calls) == 1:
                    return 402, {}, json.dumps(challenge).encode()
                return 200, {"PAYMENT-RESPONSE": "settled"}, ok_body

            with patch.object(client, "_http_call", side_effect=fake_http):
                st, _, resp = client.forward_chat_completion(
                    {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "hi"}]}
                )
            self.assertEqual(st, 200)
            self.assertEqual(json.loads(resp)["choices"][0]["message"]["content"], "OK")
            self.assertEqual(len(calls), 2)
            self.assertIn("PAYMENT-SIGNATURE", calls[1][1])
            decoded_sig = json.loads(base64.b64decode(calls[1][1]["PAYMENT-SIGNATURE"]).decode())
            self.assertEqual(
                decoded_sig["payload"]["authorization"]["from"],
                priv_to_address(test_priv),
            )
            self.assertEqual(ledger.session_spent_micro, 3200)
            lines = log_p.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["model"], "google/gemini-3.8-flash")


if __name__ == "__main__":
    unittest.main()
