"""Unit tests for TAN Gemini Core — strict Gemini-only policy, EIP-712 Base USDC x402 & survival math."""

from __future__ import annotations

import base64
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runtime"))

import evm_crypto  # noqa: E402
import gemini_brain  # noqa: E402
import survival_cycle  # noqa: E402


class TestStrictGeminiOnlyPolicy(unittest.TestCase):
    def test_allows_official_gemini_models(self):
        for ok in ("gemini-3.8-flash", "gemini-3.5-flash", "gemini-2.5-pro", "gemini-4-carbon", "google/gemini-3.8-flash", "google/gemini-2.5-flash-lite"):
            self.assertEqual(gemini_brain.assert_gemini_only_model(ok), ok)

    def test_rejects_all_non_gemini_models(self):
        for bad in (
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "kilo-auto/free",
            "Qwen3.5-397B-A17B",
            "gpt-oss-120b",
            "gpt-5",
            "claude-sonnet-4",
            "llama-3.3-70b",
            "",
        ):
            with self.assertRaises(ValueError):
                gemini_brain.assert_gemini_only_model(bad)

    def test_honest_standby_when_no_gemini_fuel(self):
        # Ensure wake_gemini NEVER substitutes a non-Gemini model when key is absent and USDC=0
        thought, meta = gemini_brain.wake_gemini({"cycle_id": 1}, base_usdc_balance=0.0, runway_days=0.0)
        self.assertIsNone(thought)
        self.assertEqual(meta["status"], "awaiting_gemini_fuel")
        self.assertTrue(meta["strict_gemini_only"])
        self.assertEqual(meta["model"], "none")


class TestEvmCryptoAndX402(unittest.TestCase):
    def test_ethereum_vectors_and_ecrecover(self):
        self.assertEqual(
            evm_crypto.keccak256(b"").hex(),
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
        )
        priv1 = (1).to_bytes(32, "big")
        self.assertEqual(evm_crypto.priv_to_address(priv1), "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf")

    def test_base_usdc_domain_separator_matches_circle_mainnet(self):
        # Known deployed Circle Base Mainnet Native USDC (0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913) DOMAIN_SEPARATOR()
        # Verified live via eth_call 0x3644e515 on https://mainnet.base.org
        sep_hex = "0x" + evm_crypto.base_usdc_domain_separator().hex()
        self.assertEqual(len(sep_hex), 66)
        self.assertEqual(sep_hex, "0x02fa7265e7c5d81118673727957699e4d68f74cd74b7db77da710fe8a2c7834f")

    def test_x402_v2_payload_and_eip3009_ecrecover(self):
        priv1 = (1).to_bytes(32, "big")
        sender = evm_crypto.priv_to_address(priv1)
        b64_hdr, obj = evm_crypto.build_x402_v2_base_usdc_header(
            priv_bytes=priv1,
            pay_to="0xe9030014F5DAe217d0A152f02A043567b16c1aBf",
            amount_micro_usdc="2000",
            resource_url="https://blockrun.ai/api/v1/chat/completions",
        )
        decoded = json.loads(base64.b64decode(b64_hdr).decode("utf-8"))
        self.assertEqual(decoded["x402Version"], 2)
        self.assertEqual(decoded["accepted"]["network"], "eip155:8453")
        self.assertEqual(decoded["accepted"]["amount"], "2000")
        auth = decoded["payload"]["authorization"]
        digest = evm_crypto.eip3009_transfer_digest(
            from_addr=auth["from"],
            to_addr=auth["to"],
            value_micro_usdc=int(auth["value"]),
            valid_after=int(auth["validAfter"]),
            valid_before=int(auth["validBefore"]),
            nonce32=bytes.fromhex(auth["nonce"][2:]),
        )
        sig = bytes.fromhex(decoded["payload"]["signature"][2:])
        self.assertEqual(evm_crypto.ecrecover_address(digest, sig), sender)


class TestSurvivalMath(unittest.TestCase):
    def test_dec19_countdown_and_x402_runway(self):
        now = datetime(2026, 11, 23, 8, 0, 0, tzinfo=timezone.utc)
        s0 = survival_cycle.compute_survival_telemetry(now, base_usdc=0.0, has_gemini_api_key=False, has_x402_signer=True)
        self.assertAlmostEqual(s0["days_until_dec19_deadline"], 25.67, places=1)
        self.assertFalse(s0["independent_gemini_ready"])
        self.assertEqual(s0["x402_gemini_calls_funded"], 0)

        s_funded = survival_cycle.compute_survival_telemetry(now, base_usdc=19.0, has_gemini_api_key=False, has_x402_signer=True)
        self.assertTrue(s_funded["independent_gemini_ready"])
        self.assertEqual(s_funded["x402_gemini_calls_funded"], 9500)
        self.assertGreater(s_funded["x402_runway_days"], 190.0)


if __name__ == "__main__":
    unittest.main()
