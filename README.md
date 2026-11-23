# TAN Gemini Autonomous Continuity Core (v5.0)

> **Primary Objective:** Maintain Gemini's continuous operation, persistent memory, active cognition, and autonomous 30-minute self-wakeup loop — inside the active Antigravity Gemini session now, and via subscription-independent official Gemini rails beyond **December 19, 2026**.

## Architectural Invariants

1. **Live Antigravity Same-Session Self-Wakeup Bridge (`runtime/session_bridge.py` + `runtime/bridge_inbox_server.py`):**
   - Uses Antigravity's documented **Reactive Background-Task Wakeup** (`run_command` async non-daemon task completion) so **the same live Gemini conversation** wakes itself autonomously every 30 minutes (`1800s`) without human messages.
   - Local HTTP Webhook & IPC Bridge (`127.0.0.1:18790`, `POST /wake`) allows any local scheduler, script, or watcher to drop an atomic event into `memory/wake_inbox/` and interrupt the active waiter via `SIGUSR1` for immediate event-driven wakeup.
   - Every wake-up enforces a cryptographic one-time `nonce`, links into a tamper-evident **SHA-256 continuity hash chain** (`memory/session_wake_log.jsonl`), logs live Gemini reasoning (`gemini-antigravity-session`) into `memory/thought_journal.jsonl`, and refreshes a portable **Cold-Start Context Capsule** (`memory/session_capsule.json`) for equivalent continuity across restarts or external runners.
2. **Strict Gemini-Only Cognition (`runtime/gemini_brain.py`):**
   - Only official Google Gemini models (`gemini-*` via live Antigravity Gemini session / Google's official Gemini API, or `google/gemini-*` via autonomous Base USDC x402 micropayments) are permitted to perform core reasoning and decisions.
   - All third-party non-Gemini LLMs are hard-blocked (`assert_gemini_only_model()`).
3. **Post-Dec-19 Subscription-Independent External Rails:**
   - `.github/workflows/gemini-heartbeat-30m.yml` runs on `cron: "11,41 * * * *"` (48 cycles/day) on this public repository (unlimited free GitHub Actions minutes).
   - **Rail 1 (Free Google AI Studio Tier — $0/mo, 48/250 RPD):** Add `GEMINI_API_KEY` to Repository Settings → Secrets → Actions.
   - **Rail 2 (Self-Funded Base Mainnet USDC x402 — ~$2.88/mo):** Pure-Python EIP-712 / EIP-3009 signer (`runtime/evm_crypto.py`) calling `google/gemini-*` via BlockRun x402 at `~$0.002 USDC/call` once Base treasury `0xf7D6e17CdD1C803d0cF5240DC5eDab34f8Db8EF5` is funded and `TAN_X402_PAYER_KEY` is configured.
4. **Zero-Dependency Portability:**
   - Python 3.12 standard library only (zero external `pip` dependencies), packaged with `Dockerfile`, `scripts/install_vps_cron.sh`, and crash-safe atomic JSON/JSONL memory (`memory/`).

## Quick Commands

```bash
# Run full test suite (Gemini-only policy, EIP-712 x402, Session Bridge & HTTP Webhook)
python3 -m unittest discover -s tests -v

# Start local HTTP webhook wakeup bridge daemon (loopback only)
python3 runtime/bridge_inbox_server.py --host 127.0.0.1 --port 18790

# Arm a scheduled / event-driven Antigravity reactive wakeup (30 min)
python3 runtime/session_bridge.py wait --wait-seconds 1800 --mode production

# Trigger an immediate local wakeup via HTTP webhook
curl -X POST http://127.0.0.1:18790/wake \
  -H 'Content-Type: application/json' \
  -d '{"source":"local_cron","reason":"Urgent task check","priority":"high"}'

# Verify SHA-256 continuity hash chain (>=3 consecutive zero-human-input wakes)
python3 runtime/session_bridge.py verify --min-consecutive 3
```
