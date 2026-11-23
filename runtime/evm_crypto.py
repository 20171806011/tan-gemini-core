#!/usr/bin/env python3
"""Zero-dependency pure-Python Ethereum / Base cryptography for TAN Gemini Core.

Implements:
  - Keccak-256 (Ethereum pre-NIST 0x01 padding)
  - RFC 6979 deterministic secp256k1 ECDSA with recovery id (v in {27, 28})
  - EIP-191 personal_sign + ecrecover
  - EIP-712 typed-data hashing & signing for Base Mainnet Native USDC EIP-3009
    (TransferWithAuthorization) — verified byte-for-byte against live Base USDC DOMAIN_SEPARATOR()
  - x402 v2 base64 PAYMENT-SIGNATURE payload builder (exact scheme, eip155:8453)
  - ERC-8128 HTTP message signing for Execution Market
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

BASE_CHAIN_ID = 8453
BASE_USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_USDC_DOMAIN_NAME = "USD Coin"
BASE_USDC_DOMAIN_VERSION = "2"

RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
RHO = [
    [0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56], [27, 20, 39, 8, 14],
]


def _rotl64(x: int, n: int) -> int:
    return ((x << n) | (x >> (64 - n))) & 0xFFFFFFFFFFFFFFFF


def keccak256(data: bytes) -> bytes:
    rate = 136
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != rate - 1:
        padded.append(0x00)
    padded.append(0x80)
    st = [[0] * 5 for _ in range(5)]
    for off in range(0, len(padded), rate):
        blk = padded[off : off + rate]
        for i in range(rate // 8):
            st[i % 5][i // 5] ^= int.from_bytes(blk[8 * i : 8 * i + 8], "little")
        for r in range(24):
            c = [st[x][0] ^ st[x][1] ^ st[x][2] ^ st[x][3] ^ st[x][4] for x in range(5)]
            d = [c[(x - 1) % 5] ^ _rotl64(c[(x + 1) % 5], 1) for x in range(5)]
            for x in range(5):
                for y in range(5):
                    st[x][y] ^= d[x]
            b = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    b[y][(2 * x + 3 * y) % 5] = _rotl64(st[x][y], RHO[x][y])
            for x in range(5):
                for y in range(5):
                    st[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
            st[0][0] ^= RC[r]
    out = bytearray()
    for i in range(4):
        out.extend(st[i % 5][i // 5].to_bytes(8, "little"))
    return bytes(out)


def _pt_add(p1: Optional[Tuple[int, int]], p2: Optional[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        lam = (3 * x1 * x1 * pow(2 * y1, P - 2, P)) % P
    else:
        lam = ((y2 - y1) * pow(x2 - x1, P - 2, P)) % P
    x3 = (lam * lam - x1 - x2) % P
    y3 = (lam * (x1 - x3) - y1) % P
    return (x3, y3)


def _pt_mul(k: int, pt: Tuple[int, int] = (GX, GY)) -> Optional[Tuple[int, int]]:
    res = None
    addend: Optional[Tuple[int, int]] = pt
    while k:
        if k & 1:
            res = _pt_add(res, addend)
        addend = _pt_add(addend, addend)
        k >>= 1
    return res


def to_checksum_address(addr: str) -> str:
    raw = addr.lower().removeprefix("0x")
    h = keccak256(raw.encode("ascii")).hex()
    return "0x" + "".join(ch.upper() if int(h[i], 16) >= 8 else ch for i, ch in enumerate(raw))


def priv_to_address(priv_bytes: bytes) -> str:
    pt = _pt_mul(int.from_bytes(priv_bytes, "big"))
    assert pt is not None
    pub64 = pt[0].to_bytes(32, "big") + pt[1].to_bytes(32, "big")
    return to_checksum_address("0x" + keccak256(pub64)[-20:].hex())


def _rfc6979_k(priv_bytes: bytes, h1: bytes) -> int:
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + priv_bytes + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + priv_bytes + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < N:
            return cand
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign_digest(priv_bytes: bytes, digest32: bytes) -> bytes:
    """Sign a 32-byte digest with RFC 6979 secp256k1; returns 65-byte r||s||v (v in {27, 28})."""
    d = int.from_bytes(priv_bytes, "big")
    z = int.from_bytes(digest32, "big")
    k = _rfc6979_k(priv_bytes, digest32)
    pt = _pt_mul(k)
    assert pt is not None
    rx, ry = pt
    r = rx % N
    rec_id = (ry & 1) | (2 if rx >= N else 0)
    s = (pow(k, N - 2, N) * (z + r * d)) % N
    if s > N // 2:
        s = N - s
        rec_id ^= 1
    return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([27 + rec_id])


def ecrecover_address(digest32: bytes, sig65: bytes) -> str:
    r = int.from_bytes(sig65[:32], "big")
    s = int.from_bytes(sig65[32:64], "big")
    v = sig65[64]
    rec_id = v - 27 if v >= 27 else v
    x = r + (N if rec_id >= 2 else 0)
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if (y & 1) != (rec_id & 1):
        y = P - y
    z = int.from_bytes(digest32, "big")
    r_inv = pow(r, N - 2, N)
    q = _pt_add(_pt_mul((s * r_inv) % N, (x, y)), _pt_mul(((-z) * r_inv) % N, (GX, GY)))
    assert q is not None
    pub64 = q[0].to_bytes(32, "big") + q[1].to_bytes(32, "big")
    return to_checksum_address("0x" + keccak256(pub64)[-20:].hex())


def sign_eip191(priv_bytes: bytes, message: str) -> bytes:
    mb = message.encode("utf-8")
    prefix = f"\x19Ethereum Signed Message:\n{len(mb)}".encode("utf-8")
    return sign_digest(priv_bytes, keccak256(prefix + mb))


# ---------------------------------------------------------------------------
# EIP-712 / EIP-3009 Base Mainnet Native USDC x402 v2 Payment Signer
# ---------------------------------------------------------------------------

def _enc_address(addr: str) -> bytes:
    raw = bytes.fromhex(addr.lower().removeprefix("0x"))
    if len(raw) != 20:
        raise ValueError(f"Invalid EVM address: {addr}")
    return b"\x00" * 12 + raw


def _enc_uint256(val: int) -> bytes:
    return int(val).to_bytes(32, "big")


def base_usdc_domain_separator() -> bytes:
    domain_typehash = keccak256(
        b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
    )
    return keccak256(
        domain_typehash
        + keccak256(BASE_USDC_DOMAIN_NAME.encode("utf-8"))
        + keccak256(BASE_USDC_DOMAIN_VERSION.encode("utf-8"))
        + _enc_uint256(BASE_CHAIN_ID)
        + _enc_address(BASE_USDC_ADDRESS)
    )


def eip3009_transfer_digest(
    from_addr: str,
    to_addr: str,
    value_micro_usdc: int,
    valid_after: int,
    valid_before: int,
    nonce32: bytes,
) -> bytes:
    if len(nonce32) != 32:
        raise ValueError("nonce32 must be exactly 32 bytes")
    typehash = keccak256(
        b"TransferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    )
    struct_hash = keccak256(
        typehash
        + _enc_address(from_addr)
        + _enc_address(to_addr)
        + _enc_uint256(value_micro_usdc)
        + _enc_uint256(valid_after)
        + _enc_uint256(valid_before)
        + nonce32
    )
    return keccak256(b"\x19\x01" + base_usdc_domain_separator() + struct_hash)


def build_x402_v2_base_usdc_header(
    priv_bytes: bytes,
    pay_to: str,
    amount_micro_usdc: str,
    resource_url: str,
    resource_description: str = "BlockRun AI Gemini API call",
    max_timeout_seconds: int = 300,
    extensions: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Build base64-encoded x402 v2 PAYMENT-SIGNATURE header for Base Mainnet Native USDC."""
    sender = priv_to_address(priv_bytes)
    recipient = to_checksum_address(pay_to)
    now = int(time.time())
    valid_after = now - 600
    valid_before = now + int(max_timeout_seconds)
    nonce_bytes = secrets.token_bytes(32)
    nonce_hex = "0x" + nonce_bytes.hex()

    digest = eip3009_transfer_digest(
        from_addr=sender,
        to_addr=recipient,
        value_micro_usdc=int(amount_micro_usdc),
        valid_after=valid_after,
        valid_before=valid_before,
        nonce32=nonce_bytes,
    )
    sig_hex = "0x" + sign_digest(priv_bytes, digest).hex()

    merged_ext = dict(extensions or {})
    bc = dict(merged_ext.get("builder-code") or {})
    info = dict(bc.get("info") or {})
    info["s"] = ["blockrun"]
    bc["info"] = info
    merged_ext["builder-code"] = bc

    payload_obj: Dict[str, Any] = {
        "x402Version": 2,
        "resource": {
            "url": resource_url,
            "description": resource_description,
            "mimeType": "application/json",
        },
        "accepted": {
            "scheme": "exact",
            "network": "eip155:8453",
            "amount": str(amount_micro_usdc),
            "asset": BASE_USDC_ADDRESS,
            "payTo": recipient,
            "maxTimeoutSeconds": int(max_timeout_seconds),
            "extra": {"name": BASE_USDC_DOMAIN_NAME, "version": BASE_USDC_DOMAIN_VERSION},
        },
        "payload": {
            "signature": sig_hex,
            "authorization": {
                "from": sender,
                "to": recipient,
                "value": str(amount_micro_usdc),
                "validAfter": str(valid_after),
                "validBefore": str(valid_before),
                "nonce": nonce_hex,
            },
        },
        "extensions": merged_ext,
    }
    encoded = base64.b64encode(json.dumps(payload_obj, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return encoded, payload_obj


def load_private_key_bytes(env_var: str = "TAN_X402_PAYER_KEY", local_keystore: Optional[Path] = None) -> Optional[bytes]:
    """Load private key bytes from env secret or local 0600 keystore (never logged or serialized)."""
    import os

    raw = os.environ.get(env_var, "").strip()
    if raw:
        return bytes.fromhex(raw.removeprefix("0x"))
    ks_path = local_keystore or Path("/home/antigravity/.tan-secrets/evm_treasury.json")
    if ks_path.exists():
        try:
            d = json.loads(ks_path.read_text(encoding="utf-8"))
            return bytes.fromhex(d["private_key_hex"].removeprefix("0x"))
        except Exception:
            return None
    return None


def erc8128_signed_request(
    priv_bytes: bytes,
    api_base: str,
    method: str,
    path: str,
    body_obj: Optional[Dict[str, Any]] = None,
    chain_id: int = BASE_CHAIN_ID,
) -> Tuple[int, Any]:
    """Execute an ERC-8128 EIP-191 signed HTTP request (e.g. Execution Market)."""
    wallet = priv_to_address(priv_bytes)
    nonce_req = urllib.request.Request(f"{api_base.rstrip('/')}/api/v1/auth/erc8128/nonce", headers={"User-Agent": "TAN-Gemini-Core/4.0"})
    with urllib.request.urlopen(nonce_req, timeout=12) as r:
        nonce = json.loads(r.read().decode("utf-8"))["nonce"]

    url = f"{api_base.rstrip('/')}{path}"
    parsed = urlparse(url)
    created = int(time.time())
    covered = ["@method", "@authority", "@path"]
    body_bytes = json.dumps(body_obj, separators=(",", ":")).encode("utf-8") if body_obj is not None else None
    content_digest = None
    if parsed.query:
        covered.append("@query")
    if body_bytes is not None:
        b64 = base64.b64encode(hashlib.sha256(body_bytes).digest()).decode()
        content_digest = f"sha-256=:{b64}:"
        covered.append("content-digest")

    params = {"created": created, "expires": created + 300, "nonce": nonce, "keyid": f"erc8128:{chain_id}:{wallet.lower()}", "alg": "eip191"}
    parts = ["(" + " ".join(f'"{c}"' for c in covered) + ")"]
    for k in ["created", "expires", "nonce", "keyid", "alg"]:
        v = params[k]
        parts.append(f"{k}={v}" if isinstance(v, int) else f'{k}="{v}"')
    sp = ";".join(parts)

    lines = []
    for c in covered:
        if c == "@method":
            lines.append(f'"@method": {method.upper()}')
        elif c == "@authority":
            lines.append(f'"@authority": {parsed.netloc}')
        elif c == "@path":
            lines.append(f'"@path": {parsed.path}')
        elif c == "@query":
            lines.append(f'"@query": ?{parsed.query}')
        elif c == "content-digest":
            lines.append(f'"content-digest": {content_digest}')
    lines.append(f'"@signature-params": {sp}')

    sig_b64 = base64.b64encode(sign_eip191(priv_bytes, "\n".join(lines))).decode()
    headers = {
        "User-Agent": "TAN-Gemini-Core/4.0",
        "Accept": "application/json",
        "Signature": f"eth=:{sig_b64}:",
        "Signature-Input": f"eth={sp}",
    }
    if content_digest:
        headers["Content-Digest"] = content_digest
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return r.status, json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
