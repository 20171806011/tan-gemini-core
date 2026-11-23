#!/usr/bin/env python3
"""
TAN Relay Bridge Helper (Solana Mainnet USDC -> Base Mainnet USDC)
==================================================================

Safe, deterministic quote & verification helper for bridging Solana Mainnet
USDC (`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`) to our Base Mainnet
treasury (`0xf7D6e17CdD1C803d0cF5240DC5eDab34f8Db8EF5`) via Relay Protocol
(`https://api.relay.link/quote`).

Read-only by default (`--quote`). Enforces:
  - Exact hardcoded treasury recipient check (prevents address poisoning).
  - Hard fee cap (`--max-fee-usd`, default $0.15).
  - Minimum output floor check before returning unsigned transaction data.
"""

from __future__ import annotations

import argparse
import json
import struct
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RELAY_QUOTE_URL = "https://api.relay.link/quote"
RELAY_STATUS_URL = "https://api.relay.link/intents/status"
SOLANA_CHAIN_ID = 792703809
BASE_CHAIN_ID = 8453
SOLANA_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BASE_USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
DEFAULT_SOLANA_WALLET = "GJhyghToEGupadoHbPVZyXMzreQb5wHSy4AvavvraRbU"
DEFAULT_BASE_TREASURY = "0xf7D6e17CdD1C803d0cF5240DC5eDab34f8Db8EF5"
RELAY_SOLANA_DEPOSITORY_PROGRAM = "99vQwtBwYtrqqD9YSXbdum3KBdxPAVxYTaQ3cfnJSrN2"
ALLOWED_SOLANA_PROGRAMS = {
    RELAY_SOLANA_DEPOSITORY_PROGRAM,
    "ComputeBudget111111111111111111111111111111",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
}


def validate_solana_instructions(
    quote: Dict[str, Any],
    amount_usdc: float,
    expected_solana_wallet: str = DEFAULT_SOLANA_WALLET,
) -> Dict[str, Any]:
    """Verify raw Solana instruction program IDs, signer pubkey, USDC mint, and LE u64 amount."""
    steps = quote.get("steps") or []
    if not steps:
        raise ValueError("Relay quote contains no steps")
    items = steps[0].get("items") or []
    if not items:
        raise ValueError("Relay quote step[0] contains no items")
    data = items[0].get("data") or {}
    instructions = data.get("instructions") or []
    if not instructions:
        raise ValueError("Relay quote step[0].items[0] contains no Solana instructions")

    expected_micro = int(round(amount_usdc * 1_000_000))
    found_depository = False

    for idx, ins in enumerate(instructions):
        prog = str(ins.get("programId") or "")
        if prog not in ALLOWED_SOLANA_PROGRAMS:
            raise ValueError(f"Disallowed Solana programId in instruction[{idx}]: {prog}")
        if prog == RELAY_SOLANA_DEPOSITORY_PROGRAM:
            keys = ins.get("keys") or []
            pubkeys = {str(k.get("pubkey") or "") for k in keys}
            signers = {str(k.get("pubkey") or "") for k in keys if k.get("isSigner")}
            if expected_solana_wallet not in signers:
                raise ValueError(f"Expected signer {expected_solana_wallet} not in depository instruction signers")
            if SOLANA_USDC_MINT not in pubkeys:
                raise ValueError(f"Solana USDC mint {SOLANA_USDC_MINT} missing from depository instruction keys")
            raw_hex = str(ins.get("data") or "")
            raw = bytes.fromhex(raw_hex)
            if len(raw) < 16:
                raise ValueError(f"Depository instruction data too short ({len(raw)} bytes)")
            encoded_micro = struct.unpack("<Q", raw[8:16])[0]
            if encoded_micro != expected_micro:
                raise ValueError(
                    f"Instruction amount mismatch: encoded={encoded_micro} != expected={expected_micro}"
                )
            found_depository = True

    if not found_depository:
        raise ValueError(f"No Relay depository instruction ({RELAY_SOLANA_DEPOSITORY_PROGRAM}) found")

    check_endpoint = str((items[0].get("check") or {}).get("endpoint") or "")
    request_id = check_endpoint.split("requestId=")[-1] if "requestId=" in check_endpoint else ""
    return {
        "instruction_count": len(instructions),
        "alt_count": len(data.get("addressLookupTableAddresses") or []),
        "verified_micro_usdc": expected_micro,
        "request_id": request_id,
    }


def validate_quote_safety(
    quote: Dict[str, Any],
    amount_usdc: float,
    expected_recipient: str = DEFAULT_BASE_TREASURY,
    expected_solana_wallet: str = DEFAULT_SOLANA_WALLET,
    max_fee_usd: float = 0.15,
    require_instructions: bool = False,
) -> Dict[str, Any]:
    """Validate Relay quote output amount, fee cap, destination chain/token, and optional Solana instructions."""
    details = quote.get("details") or {}
    cur_out = details.get("currencyOut") or {}
    out_meta = cur_out.get("currency") or {}
    out_addr = str(out_meta.get("address") or "")
    out_chain = int(out_meta.get("chainId") or 0)
    out_amount = float(cur_out.get("amountFormatted") or 0.0)
    recipient = str(details.get("recipient") or "")

    if out_chain != BASE_CHAIN_ID:
        raise ValueError(f"Unexpected destination chainId: {out_chain} (expected {BASE_CHAIN_ID})")
    if out_addr.lower() != BASE_USDC_ADDRESS.lower():
        raise ValueError(f"Unexpected destination asset: {out_addr} (expected {BASE_USDC_ADDRESS})")
    if recipient and recipient.lower() != expected_recipient.lower():
        raise ValueError(f"Recipient mismatch: {recipient} != {expected_recipient}")

    implied_fee = round(amount_usdc - out_amount, 6)
    if implied_fee > max_fee_usd or out_amount <= 0:
        raise ValueError(
            f"Bridge fee ${implied_fee:.4f} exceeds max_fee_usd ${max_fee_usd:.4f} "
            f"(in=${amount_usdc:.4f}, out=${out_amount:.4f})"
        )

    steps = quote.get("steps") or []
    summary: Dict[str, Any] = {
        "safe": True,
        "amount_in_usdc": round(amount_usdc, 6),
        "amount_out_usdc": round(out_amount, 6),
        "implied_fee_usd": implied_fee,
        "time_estimate_sec": details.get("timeEstimate"),
        "recipient": expected_recipient,
        "step_count": len(steps),
    }

    has_instructions = bool(
        steps and (steps[0].get("items") or []) and ((steps[0]["items"][0].get("data") or {}).get("instructions"))
    )
    if require_instructions or has_instructions:
        summary["solana_instruction_check"] = validate_solana_instructions(
            quote,
            amount_usdc=amount_usdc,
            expected_solana_wallet=expected_solana_wallet,
        )
    return summary


def fetch_relay_quote(
    amount_usdc: float,
    solana_wallet: str = DEFAULT_SOLANA_WALLET,
    base_recipient: str = DEFAULT_BASE_TREASURY,
    max_fee_usd: float = 0.15,
    timeout: int = 15,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    amount_micro = str(int(round(amount_usdc * 1_000_000)))
    payload = {
        "user": solana_wallet,
        "recipient": base_recipient,
        "originChainId": SOLANA_CHAIN_ID,
        "destinationChainId": BASE_CHAIN_ID,
        "originCurrency": SOLANA_USDC_MINT,
        "destinationCurrency": BASE_USDC_ADDRESS,
        "amount": amount_micro,
        "tradeType": "EXACT_INPUT",
    }
    req = urllib.request.Request(
        RELAY_QUOTE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "tan-relay-bridge/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        quote = json.loads(resp.read().decode("utf-8"))
    summary = validate_quote_safety(
        quote,
        amount_usdc=amount_usdc,
        expected_recipient=base_recipient,
        expected_solana_wallet=solana_wallet,
        max_fee_usd=max_fee_usd,
        require_instructions=True,
    )
    return summary, quote


def fetch_relay_status(request_id: str, timeout: int = 15) -> Dict[str, Any]:
    url = f"{RELAY_STATUS_URL}?requestId={request_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "tan-relay-bridge/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="TAN Relay Solana->Base USDC Quote & Safety Helper")
    parser.add_argument("--amount-usdc", type=float, default=4.0, help="Amount of Solana USDC to quote")
    parser.add_argument("--max-fee-usd", type=float, default=0.15, help="Hard fee cap in USD")
    parser.add_argument("--save-quote", type=str, default="", help="Optional path to save validated quote bundle JSON")
    parser.add_argument("--status", type=str, default="", help="Poll Relay intent status for a given requestId")
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps(fetch_relay_status(args.status), indent=2))
        return 0

    summary, quote = fetch_relay_quote(amount_usdc=args.amount_usdc, max_fee_usd=args.max_fee_usd)
    if args.save_quote:
        out_path = Path(args.save_quote)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps({"summary": summary, "quote": quote}, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

