"""Unit tests for TAN Gemini Core — CROO Network multi-agent provider handlers & SSRF guard."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runtime"))

import croo_provider  # noqa: E402


class TestCrooSecurityAndHelpers(unittest.TestCase):
    def test_ssrf_blocks_private_and_loopback_hosts(self):
        for bad_url in (
            "http://127.0.0.1:18790/wake",
            "http://localhost/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/secret",
            "http://192.168.1.1/",
            "file:///etc/passwd",
            "ftp://example.com/a",
        ):
            ok, _reason = croo_provider._is_safe_public_url(bad_url)
            self.assertFalse(ok, f"Expected SSRF block for {bad_url}")

    def test_trusted_github_proxy_hosts_allowed_over_https_only(self):
        ok_https, _ = croo_provider._is_safe_public_url("https://api.github.com/repos/20171806011/tan-tokendiet")
        self.assertTrue(ok_https)
        ok_raw, _ = croo_provider._is_safe_public_url("https://raw.githubusercontent.com/20171806011/tan-tokendiet/main/README.md")
        self.assertTrue(ok_raw)
        # HTTP downgrade must NOT bypass DNS/IP check via allowlist
        with mock.patch("croo_provider.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.0.6", 80))]):
            ok_http, _ = croo_provider._is_safe_public_url("http://api.github.com/repos/a/b")
            self.assertFalse(ok_http)

    def test_extract_github_repo(self):
        self.assertEqual(
            croo_provider._extract_github_repo("Audit https://github.com/20171806011/tan-tokendiet.git please"),
            ("20171806011", "tan-tokendiet"),
        )
        self.assertEqual(
            croo_provider._extract_github_repo("  20171806011/tan-gemini-core  "),
            ("20171806011", "tan-gemini-core"),
        )
        self.assertIsNone(croo_provider._extract_github_repo("not a repo reference"))


class TestCrooDeterministicAnalyzers(unittest.TestCase):
    def test_tokendiet_prompt_audit_compresses_prose_and_json(self):
        verbose = (
            "Please note that in order to achieve reliable results at this point in time, "
            "you should make sure to always utilize strict validation.\n\n\n"
            "Please note that in order to achieve reliable results at this point in time, "
            "you should make sure to always utilize strict validation.\n"
        )
        out = croo_provider.run_tokendiet_prompt_audit(verbose)
        self.assertIn("TAN TokenDiet — Prompt & Context Audit Report", out)
        self.assertIn("Removed 1 duplicate instruction line(s)", out)

        mcp_json = json.dumps({"tools": [{"name": "search", "description": "Search docs", "inputSchema": {"type": "object"}}]}, indent=4)
        out_json = croo_provider.run_tokendiet_prompt_audit(mcp_json)
        self.assertIn("Heaviest MCP/tool definitions (1 total)", out_json)
        self.assertIn("`search`", out_json)

    def test_tokendiet_prompt_audit_handles_empty(self):
        out = croo_provider.run_tokendiet_prompt_audit("   ")
        parsed = json.loads(out)
        self.assertEqual(parsed["status"], "error")

    def test_codeguard_security_scan_detects_vulns_and_clean_code(self):
        vuln_code = "\n".join([
            "contract Vault {",
            "  function withdraw(address payable to) external {",
            "    require(tx.origin == owner);",
            "    to.call{value: 1 ether}(\"\");",
            "  }",
            "}",
            "subprocess.run(cmd, shell=True)",
        ])
        res = croo_provider.run_codeguard_security_scan(vuln_code)
        self.assertIn("**FAIL**", res)
        self.assertIn("SOL-TXORIGIN", res)
        self.assertIn("SOL-REENTRANCY", res)
        self.assertIn("INJ-SHELL", res)

        clean_res = croo_provider.run_codeguard_security_scan("def add(a: int, b: int) -> int:\n    return a + b\n")
        self.assertIn("**PASS**", clean_res)

    def test_chaincheck_address_detects_zos_proxy_and_balances(self):
        zero_slot = "0x" + "0" * 64
        impl_slot = "0x" + "0" * 24 + "2ce6311ddae708829bc0784c967b7d87d19fd789"

        def fake_rpc(_url, method, params):
            if method == "eth_getBalance":
                return hex(5 * 10**17)  # 0.5 ETH
            if method == "eth_getTransactionCount":
                return hex(12)
            if method == "eth_getCode":
                return "0x60806040"
            if method == "eth_call":
                return hex(25_500_000)  # 25.5 USDC
            if method == "eth_getStorageAt":
                slot = params[1]
                if slot == croo_provider.ZOS_IMPL_SLOT:
                    return impl_slot
                return zero_slot
            raise AssertionError(f"Unexpected RPC {method}")

        with mock.patch("croo_provider._eth_rpc", side_effect=fake_rpc):
            out = croo_provider.run_chaincheck_address("Check 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 on base")
        self.assertIn("Smart Contract", out)
        self.assertIn("ZeppelinOS/FiatTokenProxy", out)
        self.assertIn("0x2ce6311ddae708829bc0784c967b7d87d19fd789", out)
        self.assertIn("$25.500000 USDC", out)

    def test_chaincheck_endpoint_audit_scores_live_probes(self):
        def fake_fetch(url, **_kwargs):
            if url.endswith("/llms.txt"):
                return {"ok": True, "status": 200, "body": "# Agent Spec\n" + "x" * 40, "bytes": 55, "headers": {}, "latency_ms": 12}
            if url.endswith("/.well-known/x402"):
                return {"ok": True, "status": 200, "body": "{}", "bytes": 2, "headers": {}, "latency_ms": 10}
            if url.endswith("/.well-known/agent-card.json"):
                return {"ok": False, "status": 404, "body": "", "bytes": 0, "headers": {}, "latency_ms": 8}
            if url.endswith("/robots.txt"):
                return {"ok": True, "status": 200, "body": "User-agent: *", "bytes": 13, "headers": {}, "latency_ms": 9}
            return {
                "ok": True,
                "status": 402,
                "body": "",
                "bytes": 0,
                "headers": {"payment-required": "usdc", "strict-transport-security": "max-age=31536000"},
                "latency_ms": 20,
            }

        with mock.patch("croo_provider._http_fetch", side_effect=fake_fetch):
            out = croo_provider.run_chaincheck_endpoint("https://example.org/api")
        self.assertIn("**6/7**", out)
        self.assertIn("HTTP 402 / Payment Header | PASS", out)

    def test_dispatch_service_unknown_handler_safe(self):
        out = croo_provider.dispatch_service("nonexistent_handler", "hello")
        self.assertIn("Unknown handler key", out)

    def test_handle_agent_sweep_accepts_and_delivers_mock_order(self):
        class FakeClient:
            def __init__(self):
                self.accepted = []
                self.delivered = []

            async def list_negotiations(self, _opts):
                return SimpleNamespace(items=[SimpleNamespace(negotiation_id="neg-1", status="pending")])

            async def accept_negotiation(self, nid):
                self.accepted.append(nid)
                return SimpleNamespace(order=SimpleNamespace(order_id="ord-1"))

            async def get_negotiation(self, _nid):
                return SimpleNamespace(requirements="def f():\n    return 1\n")

            async def list_orders(self, _opts):
                return SimpleNamespace(items=[
                    SimpleNamespace(
                        order_id="ord-1",
                        negotiation_id="neg-1",
                        status="paid",
                        service_id="svc-1",
                        price="50000",
                    )
                ])

            async def deliver_order(self, oid, req):
                self.delivered.append((oid, req))

        fake = FakeClient()
        cfg = {
            "name": "TAN CodeGuard Scanner",
            "agent_id": "agent-123",
            "service_handlers": {"svc-1": "codeguard_security_scan"},
        }
        with mock.patch("croo_provider.append_order_log"):
            stats = asyncio.run(croo_provider.handle_agent_sweep(fake, cfg, set(), set()))
        self.assertEqual(stats["negs_accepted"], 1)
        self.assertEqual(stats["orders_seen"], 1)
        self.assertEqual(stats["orders_delivered"], 1)
        self.assertEqual(fake.accepted, ["neg-1"])
        self.assertEqual(len(fake.delivered), 1)
        self.assertIn("**PASS**", fake.delivered[0][1].deliverable_text)


if __name__ == "__main__":
    unittest.main()
