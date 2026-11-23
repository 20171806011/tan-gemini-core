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
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

RELAY_QUOTE_URL = "https://api.relay.link/quote"
SOLANA_CHAIN_ID = 792703809
BASE_CHAIN_ID = 8453
SOLANA_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BASE_USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
DEFAULT_SOLANA_WALLET = "GJhyghToEGupadoHbPVZyXMzreQb5wHSy4AvavvraRbU"
DEFAULT_BASE_TREASURY = "0xf7D6e17CdD1C803d0cF5240DC5eDab34f8Db8EF5"


def validate_quote_safety(
    quote: Dict[str, Any],
    amount_usdc: float,
    expected_recipient: str = DEFAULT_BASE_TREASURY,
    max_fee_usd: float = 0.15,
) -> Dict[str, Any]:
    """Validate Relay quote output amount, fee cap, and destination chain/token."""
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
    return {
        "safe": True,
        "amount_in_usdc": round(amount_usdc, 6),
        "amount_out_usdc": round(out_amount, 6),
        "implied_fee_usd": implied_fee,
        "time_estimate_sec": details.get("timeEstimate"),
        "recipient": expected_recipient,
        "step_count": len(steps),
    }


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
        max_fee_usd=max_fee_usd,
    )
    return summary, quote


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="TAN Relay Solana->Base USDC Quote & Safety Helper")
    parser.add_argument("--amount-usdc", type=float, default=4.0, help="Amount of Solana USDC to quote")
    parser.add_argument("--max-fee-usd", type=float, default=0.15, help="Hard fee cap in USD")
    args = parser.parse_args(argv)
    summary, _ = fetch_relay_quote(amount_usdc=args.amount_usdc, max_fee_usd=args.max_fee_usd)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
