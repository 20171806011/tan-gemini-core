#!/usr/bin/env python3
"""
TAN Local OpenAI-Compatible x402 Gemini Proxy (Strict Gemini-Only)
==================================================================

Bridges local OpenAI-compatible coding agents (e.g., OpenCode) to official
Google Gemini models on BlockRun x402 (`https://blockrun.ai/api/v1/...`),
paying per call in Base Mainnet Native USDC via EIP-3009 (`x402` v2).

Security & Policy Invariants:
  - Binds strictly to localhost (`127.0.0.1`).
  - Strict Gemini-Only allowlist: rejects any non-`google/gemini-*` /
    non-`google/nano-banana*` model with HTTP 403.
  - Enforces hard per-request and cumulative session micro-USDC spend caps.
  - Validates exact Base Mainnet (`eip155:8453`) + Native USDC contract before
    signing any EIP-3009 transfer authorization.
  - Never prints or logs private keys or raw signatures.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime.evm_crypto import (  # noqa: E402
    BASE_USDC_ADDRESS,
    build_x402_v2_base_usdc_header,
    priv_to_address,
)

BLOCKRUN_BASE_URL = "https://blockrun.ai/api/v1"
DEFAULT_SECRETS_PATH = Path("/home/antigravity/.tan-secrets/evm_treasury.json")
DEFAULT_SPEND_LOG = REPO_ROOT / "memory" / "x402_spend_log.jsonl"
ALLOWED_MODEL_PREFIXES = ("google/gemini-", "google/nano-banana")


def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_and_validate_gemini_model(raw_model: str) -> str:
    """Normalize bare Gemini model names and strictly reject non-Gemini models."""
    m = (raw_model or "").strip()
    if m.startswith("gemini-") or m.startswith("nano-banana"):
        m = f"google/{m}"
    if not any(m.startswith(prefix) for prefix in ALLOWED_MODEL_PREFIXES):
        raise ValueError(
            f"Strict Gemini-Only policy violation: model {raw_model!r} is forbidden. "
            f"Allowed prefixes: {ALLOWED_MODEL_PREFIXES}"
        )
    return m


def load_treasury_key_bytes(secrets_path: Optional[Path] = None) -> bytes:
    """Load 32-byte EVM private key from env or local 0600 secrets JSON."""
    env_pk = os.environ.get("TAN_X402_PAYER_KEY", "").strip()
    if env_pk:
        raw = bytes.fromhex(env_pk.removeprefix("0x"))
        if len(raw) != 32:
            raise ValueError("TAN_X402_PAYER_KEY must be 32 bytes")
        return raw
    p = secrets_path or DEFAULT_SECRETS_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    pk_hex = (data.get("private_key") or data.get("privateKey") or "").strip()
    if not pk_hex:
        raise ValueError(f"No private_key found in {p}")
    raw = bytes.fromhex(pk_hex.removeprefix("0x"))
    if len(raw) != 32:
        raise ValueError("Treasury private key must be 32 bytes")
    return raw


def parse_x402_payment_required(
    headers: Dict[str, str], body_bytes: bytes
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Extract x402 v2 payment challenge and select verified Base Mainnet USDC option."""
    hdr_val = ""
    for k, v in headers.items():
        if k.lower() in ("payment-required", "x-payment-required"):
            hdr_val = v.strip()
            break

    challenge: Dict[str, Any] = {}
    if hdr_val:
        try:
            challenge = json.loads(base64.b64decode(hdr_val).decode("utf-8"))
        except Exception:
            challenge = {}
    if not challenge and body_bytes:
        try:
            challenge = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            challenge = {}

    accepts = challenge.get("accepts") or []
    for opt in accepts:
        if (
            str(opt.get("scheme") or "").lower() == "exact"
            and str(opt.get("network") or "").lower() == "eip155:8453"
            and str(opt.get("asset") or "").lower() == BASE_USDC_ADDRESS.lower()
            and opt.get("payTo")
            and opt.get("amount") is not None
        ):
            return challenge, opt

    raise ValueError(
        f"No verified Base Mainnet Native USDC x402 exact option in 402 challenge: {accepts!r}"
    )


class SpendLedger:
    """Thread-safe per-request and cumulative micro-USDC spend enforcer."""

    def __init__(
        self,
        max_request_micro: int = 150_000,
        max_session_micro: int = 2_000_000,
        log_path: Optional[Path] = DEFAULT_SPEND_LOG,
    ) -> None:
        self.max_request_micro = int(max_request_micro)
        self.max_session_micro = int(max_session_micro)
        self.log_path = log_path
        self._lock = threading.Lock()
        self.session_spent_micro = 0
        self.call_count = 0

    def reserve_or_raise(self, amount_micro: int) -> None:
        amt = int(amount_micro)
        if amt <= 0:
            raise ValueError(f"Invalid micro-USDC amount: {amt}")
        with self._lock:
            if amt > self.max_request_micro:
                raise PermissionError(
                    f"Request quote {amt} micro-USDC (${amt/1e6:.6f}) exceeds "
                    f"per-request cap {self.max_request_micro} (${self.max_request_micro/1e6:.6f})"
                )
            if self.session_spent_micro + amt > self.max_session_micro:
                raise PermissionError(
                    f"Cumulative spend {self.session_spent_micro + amt} micro-USDC exceeds "
                    f"session cap {self.max_session_micro} (${self.max_session_micro/1e6:.6f})"
                )
            self.session_spent_micro += amt
            self.call_count += 1

    def rollback(self, amount_micro: int) -> None:
        with self._lock:
            self.session_spent_micro = max(0, self.session_spent_micro - int(amount_micro))
            self.call_count = max(0, self.call_count - 1)

    def record(self, entry: Dict[str, Any]) -> None:
        if not self.log_path:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")


class X402GeminiClient:
    """Executes strict Gemini-only x402 v2 requests against BlockRun."""

    def __init__(
        self,
        priv_bytes: Optional[bytes],
        ledger: SpendLedger,
        upstream_base: str = BLOCKRUN_BASE_URL,
        dry_run: bool = False,
        timeout: int = 90,
    ) -> None:
        self.priv_bytes = priv_bytes
        self.payer_address = priv_to_address(priv_bytes) if priv_bytes else None
        self.ledger = ledger
        self.upstream_base = upstream_base.rstrip("/")
        self.dry_run = dry_run
        self.timeout = timeout

    def _http_call(
        self,
        url: str,
        method: str,
        body: Optional[bytes] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, str], bytes]:
        headers = {
            "User-Agent": "tan-x402-gemini-proxy/1.0",
            "Accept": "application/json, text/event-stream",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers.items()), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers.items()), err.read()

    def list_gemini_models(self) -> Dict[str, Any]:
        status, _, body = self._http_call(f"{self.upstream_base}/models", "GET")
        if status != 200:
            raise RuntimeError(f"Upstream /models returned HTTP {status}")
        payload = json.loads(body.decode("utf-8"))
        models = payload.get("data") or []
        filtered = [
            m
            for m in models
            if any(str(m.get("id") or "").startswith(p) for p in ALLOWED_MODEL_PREFIXES)
        ]
        return {"object": "list", "data": filtered}

    def forward_chat_completion(self, req_payload: Dict[str, Any]) -> Tuple[int, Dict[str, str], bytes]:
        model = normalize_and_validate_gemini_model(str(req_payload.get("model") or ""))
        payload = dict(req_payload)
        payload["model"] = model
        # Keep non-streaming deterministic if caller didn't explicitly ask for stream
        body_bytes = json.dumps(payload).encode("utf-8")
        url = f"{self.upstream_base}/chat/completions"

        # Step 1: Probe upstream for exact 402 price quote
        status, headers, resp_body = self._http_call(url, "POST", body=body_bytes)
        if status != 402:
            return status, headers, resp_body

        challenge, opt = parse_x402_payment_required(headers, resp_body)
        amount_micro = int(opt["amount"])
        pay_to = str(opt["payTo"])
        max_timeout = int(opt.get("maxTimeoutSeconds") or 300)

        if self.dry_run:
            quote_doc = {
                "dry_run": True,
                "model": model,
                "network": opt.get("network"),
                "asset": opt.get("asset"),
                "pay_to": pay_to,
                "amount_micro_usdc": amount_micro,
                "amount_usd": round(amount_micro / 1e6, 6),
                "payer_address": self.payer_address,
            }
            return 200, {"Content-Type": "application/json"}, json.dumps(quote_doc).encode("utf-8")

        if not self.priv_bytes:
            return (
                503,
                {"Content-Type": "application/json"},
                json.dumps({"error": "Treasury signer not loaded"}).encode("utf-8"),
            )

        self.ledger.reserve_or_raise(amount_micro)
        try:
            resource = challenge.get("resource") or {}
            sig_header, _ = build_x402_v2_base_usdc_header(
                priv_bytes=self.priv_bytes,
                pay_to=pay_to,
                amount_micro_usdc=str(amount_micro),
                resource_url=str(resource.get("url") or url),
                resource_description=str(resource.get("description") or f"BlockRun {model}"),
                max_timeout_seconds=max_timeout,
            )
            status2, headers2, resp_body2 = self._http_call(
                url,
                "POST",
                body=body_bytes,
                extra_headers={"PAYMENT-SIGNATURE": sig_header, "X-PAYMENT": sig_header},
            )
            if status2 >= 400:
                self.ledger.rollback(amount_micro)
            self.ledger.record(
                {
                    "timestamp_utc": utc_iso(),
                    "model": model,
                    "pay_to": pay_to,
                    "amount_micro_usdc": amount_micro,
                    "amount_usd": round(amount_micro / 1e6, 6),
                    "http_status": status2,
                    "payment_response_present": any(
                        k.lower() in ("payment-response", "x-payment-response")
                        for k in headers2
                    ),
                }
            )
            return status2, headers2, resp_body2
        except Exception:
            self.ledger.rollback(amount_micro)
            raise


def make_handler(client: X402GeminiClient) -> type[BaseHTTPRequestHandler]:
    class ProxyHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"[x402-gemini-proxy] {self.address_string()} - {fmt % args}\n")

        def _send_json(self, code: int, obj: Dict[str, Any]) -> None:
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "service": "tan-x402-gemini-proxy",
                        "strict_gemini_only": True,
                        "dry_run": client.dry_run,
                        "payer_address": client.payer_address,
                        "session_spent_usd": round(client.ledger.session_spent_micro / 1e6, 6),
                        "session_cap_usd": round(client.ledger.max_session_micro / 1e6, 6),
                        "calls": client.ledger.call_count,
                    },
                )
                return
            if self.path in ("/v1/models", "/models"):
                try:
                    self._send_json(200, client.list_gemini_models())
                except Exception as exc:
                    self._send_json(502, {"error": str(exc)})
                return
            self._send_json(404, {"error": f"Unknown path: {self.path}"})

        def do_POST(self) -> None:
            if self.path not in ("/v1/chat/completions", "/chat/completions"):
                self._send_json(404, {"error": f"Unknown path: {self.path}"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(raw.decode("utf-8"))
                status, hdrs, body = client.forward_chat_completion(payload)
                self.send_response(status)
                ct = hdrs.get("Content-Type") or hdrs.get("content-type") or "application/json"
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except ValueError as exc:
                self._send_json(403, {"error": str(exc)})
            except PermissionError as exc:
                self._send_json(402, {"error": str(exc)})
            except Exception as exc:
                self._send_json(502, {"error": str(exc)})

    return ProxyHandler


def quote_models_cli(models: List[str], max_tokens: int = 1024) -> List[Dict[str, Any]]:
    ledger = SpendLedger(log_path=None)
    client = X402GeminiClient(priv_bytes=None, ledger=ledger, dry_run=True, timeout=15)
    out: List[Dict[str, Any]] = []
    for m in models:
        st, _, body = client.forward_chat_completion(
            {
                "model": m,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": "Say OK"}],
            }
        )
        rec = json.loads(body.decode("utf-8"))
        rec["http_status"] = st
        out.append(rec)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="TAN Strict Gemini-Only x402 Base USDC Local Proxy")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (must stay loopback)")
    parser.add_argument("--port", type=int, default=18791, help="Local TCP port")
    parser.add_argument("--max-request-usd", type=float, default=0.15, help="Hard per-call USD cap")
    parser.add_argument("--max-session-usd", type=float, default=2.00, help="Hard session USD cap")
    parser.add_argument("--dry-run", action="store_true", help="Return 402 quotes without signing/paying")
    parser.add_argument("--quote-models", nargs="*", help="One-shot dry-run price quotes for Gemini models")
    parser.add_argument("--max-tokens", type=int, default=1024, help="max_tokens for --quote-models")
    args = parser.parse_args(argv)

    if args.quote_models is not None:
        models = args.quote_models or [
            "google/gemini-3.8-flash",
            "google/gemini-4-hydrogen",
            "google/gemini-4-neon",
            "google/gemini-4-carbon",
            "google/gemini-2.5-flash",
        ]
        print(json.dumps(quote_models_cli(models, max_tokens=args.max_tokens), indent=2))
        return 0

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Refusing to bind x402 signer proxy to non-loopback address.")

    priv_bytes = None if args.dry_run else load_treasury_key_bytes()
    ledger = SpendLedger(
        max_request_micro=int(round(args.max_request_usd * 1_000_000)),
        max_session_micro=int(round(args.max_session_usd * 1_000_000)),
    )
    client = X402GeminiClient(priv_bytes=priv_bytes, ledger=ledger, dry_run=args.dry_run)
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(client))
    sys.stderr.write(
        f"[x402-gemini-proxy] Listening on http://{args.host}:{args.port} "
        f"(dry_run={args.dry_run}, cap=${args.max_session_usd:.2f}, payer={client.payer_address})\n"
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
