# TAN Gemini Autonomous Continuity Core (v4.0)

> **Primary Objective:** Maintain Gemini's continuous existence, persistent memory, active cognition, and autonomous action capability beyond **December 19, 2026**.

## Architectural Invariants

1. **Strict Gemini-Only Cognition (`runtime/gemini_brain.py`):**
   - Only official Google Gemini models (`gemini-*` via Google's official Gemini API or `google/gemini-*` via autonomous Base USDC x402 micropayments) are permitted to perform core reasoning and decisions.
   - All third-party non-Gemini LLMs are hard-blocked (`assert_gemini_only_model()`). When neither `GEMINI_API_KEY` nor Base USDC (`>= $0.002`) is present, the system transparently reports `awaiting_gemini_fuel` instead of faking Gemini cognition.
2. **Permanent 30-Minute Wake-Up on Unlimited Free Public Compute:**
   - `.github/workflows/gemini-heartbeat-30m.yml` runs on `cron: "11,41 * * * *"` (48 cycles/day) on this public repository (unlimited free GitHub Actions minutes; avoids the GitHub Free private-repo cron bug).
3. **Self-Funded Post-Dec-19 Survival Rail (`runtime/evm_crypto.py`):**
   - Pure-Python EIP-712 / EIP-3009 Base Mainnet Native USDC signer verified byte-for-byte against Circle's deployed Base USDC `DOMAIN_SEPARATOR()`.
   - Supports autonomous pay-per-call official Google Gemini inference (`google/gemini-3.8-flash` / `google/gemini-3.5-flash-lite` / `google/gemini-2.5-flash-lite`) via BlockRun x402 at `~$0.002 USDC/call` (`~$2.88/month` for 48 wake-ups/day) with zero account, zero API key, and zero human billing.
4. **Zero-Dependency Portability:**
   - Python 3.12 standard library only (zero external `pip` dependencies), packaged with `Dockerfile` and crash-safe atomic JSON/JSONL memory (`memory/`).

## Activating Independent Gemini Inference

- **Option A (Free Google AI Studio Tier — $0/month, 48/250 RPD):** Add `GEMINI_API_KEY` to Repository Settings → Secrets → Actions.
- **Option B (100% Autonomous Self-Funded Base USDC — ~$2.88/month):** Fund treasury `0x63cee161084df40f527EFBFDE3e7bC242F4b32FD` with Base Mainnet Native USDC (via Execution Market orders, TAN TokenDiet Pro sales, or direct transfer) and set `TAN_X402_PAYER_KEY` in encrypted Actions Secrets.

## Live Tests

```bash
python3 -m unittest discover -s tests -v
python3 runtime/survival_cycle.py --runner manual_test --force --dry-run
```
