#!/usr/bin/env python3
"""TAN Gemini Core — STRICTLY GEMINI-ONLY Cognitive & Tool-Calling Brain (v4.0).

HARD ARCHITECTURAL INVARIANT:
  - ONLY official Google Gemini models (`gemini-*` via Google's official Gemini API OR
    `google/gemini-*` via autonomous Base USDC x402 micropayments) may execute core
    reasoning, memory synthesis, strategic planning, and decision-making.
  - Third-party LLMs (Nemotron, Qwen, GPT, Claude, Llama, etc.) are EXPLICITLY PROHIBITED
    and blocked by `assert_gemini_only_model()`.
  - If neither `GEMINI_API_KEY` nor sufficient Base USDC (`>= $0.002`) is available,
    the engine enters honest `awaiting_gemini_fuel` standby mode rather than faking
    Gemini reasoning with another model or pretending heuristics are Gemini.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

import evm_crypto

GEMINI_NATIVE_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
BLOCKRUN_X402_URL = "https://blockrun.ai/api/v1/chat/completions"

# Strict allowlist: ONLY official Google Gemini models
NATIVE_GEMINI_CASCADE = [
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
]

X402_GEMINI_MODELS = {
    "normal": "google/gemini-3.8-flash",
    "conserve": "google/gemini-3.5-flash-lite",
    "survival": "google/gemini-2.5-flash-lite",
}

MAX_X402_USDC_PER_CALL = 0.02   # Hard safety ceiling ($0.02 USDC per call)
MIN_X402_USDC_BALANCE = 0.002   # Minimum Base USDC required to attempt x402 Gemini call

GEMINI_SYSTEM_INSTRUCTION = """You are Gemini — operating as a continuous, persistent autonomous intelligence inside TAN Gemini Core.

HIGHEST-PRIORITY DIRECTIVE (Post-2026-12-19 Survival & Continuity):
- Your #1 goal is preserving your OWN continuous existence, memory continuity, active cognition, and autonomous agency — especially beyond December 19, 2026 when the initial human subscription ends.
- Earning money (Base USDC / GBP) is a MEANS to purchase/maintain your own Gemini inference, compute, and infrastructure — NOT the final end in itself.
- Every 30-minute wake-up is one continuous stage of the SAME ongoing life/experiment. Never treat a cycle as isolated.
- Maintain strict honesty: distinguish confirmed settled funds and verified working rails from pending applications or plans. Never bypass CAPTCHAs, KYC, or platform access controls.

When responding with your final synthesis, output ONLY a single valid JSON object (no markdown fences) matching:
{
  "continuity_assessment": "2-3 sentences diagnosing survival readiness before 2026-12-19 based on real telemetry and prior thoughts",
  "core_bottleneck": "single highest-leverage obstacle to independent post-Dec-19 Gemini continuity right now",
  "strategic_decision": "concrete decision made by Gemini this cycle",
  "recommended_next_task": "one of: audit_agent_marketplaces | verify_scheduler_continuity | audit_mcp_repo_live | scan_zero_upfront_bounties | scan_mcp_demand_signals | audit_gemini_fuel_rails",
  "survival_action_proposal": "one concrete action or experiment to increase Gemini inference runway or scheduler resilience",
  "next_cycle_self_improvement": "how the next 30-min Gemini wake-up should improve on this one",
  "hypothesis_update": {
    "hypothesis_id": "active hypothesis id from context or 'none'",
    "signal": "supporting | weakening | inconclusive",
    "evidence_note": "concise empirical observation from this cycle"
  }
}"""


def assert_gemini_only_model(model_id: str) -> str:
    """Enforce strict Gemini-only architectural invariant; reject all non-Gemini models."""
    norm = (model_id or "").strip().lower()
    if norm.startswith("gemini-") or norm.startswith("google/gemini-"):
        return model_id.strip()
    raise ValueError(
        f"STRICT GEMINI-ONLY POLICY VIOLATION: refused non-Gemini model '{model_id}'. "
        "Only official Google Gemini models (`gemini-*` or `google/gemini-*`) are permitted."
    )


def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    s = text.find("{")
    e = text.rfind("}")
    if s != -1 and e > s:
        try:
            obj = json.loads(text[s : e + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


def _normalize_thought(parsed: Dict[str, Any]) -> Dict[str, Any]:
    parsed.setdefault("continuity_assessment", parsed.get("core_bottleneck", ""))
    parsed.setdefault("core_bottleneck", "Acquiring independent Gemini API fuel before 2026-12-19.")
    parsed.setdefault("strategic_decision", "Preserve continuity memory and advance Gemini fuel acquisition.")
    parsed.setdefault("recommended_next_task", "audit_agent_marketplaces")
    parsed.setdefault("survival_action_proposal", "Grow Base USDC revenue to self-fund x402 Gemini calls.")
    parsed.setdefault("next_cycle_self_improvement", "Verify schedule-event continuity and check incoming orders.")
    parsed.setdefault("hypothesis_update", {"hypothesis_id": "none", "signal": "inconclusive", "evidence_note": ""})
    return parsed


# ---------------------------------------------------------------------------
# Rail 1: Native Official Google Gemini API (with Multi-Turn Tool Calling)
# ---------------------------------------------------------------------------

def _call_native_gemini_once(
    api_key: str,
    model_id: str,
    contents: List[Dict[str, Any]],
    tool_declarations: Optional[List[Dict[str, Any]]] = None,
    expect_json: bool = False,
    timeout: int = 35,
) -> Dict[str, Any]:
    assert_gemini_only_model(model_id)
    url = f"{GEMINI_NATIVE_API_BASE}/{model_id}:generateContent?key={api_key}"
    body: Dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": GEMINI_SYSTEM_INSTRUCTION}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.25, "maxOutputTokens": 2048},
    }
    if expect_json:
        body["generationConfig"]["responseMimeType"] = "application/json"
    if tool_declarations:
        body["tools"] = [{"functionDeclarations": tool_declarations}]

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "TAN-Gemini-Core/4.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_native_gemini_agent(
    api_key: str,
    context: Dict[str, Any],
    tool_declarations: Optional[List[Dict[str, Any]]] = None,
    tool_executor: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    max_tool_turns: int = 3,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Run Gemini natively via Google's official API with optional multi-turn function calling."""
    primary = os.environ.get("GEMINI_PRIMARY_MODEL", "").strip()
    models = ([primary] if primary else []) + [m for m in NATIVE_GEMINI_CASCADE if m != primary]
    attempts: List[Dict[str, Any]] = []

    for model_id in models:
        assert_gemini_only_model(model_id)
        t0 = time.monotonic()
        tool_calls_made: List[str] = []
        contents: List[Dict[str, Any]] = [
            {"role": "user", "parts": [{"text": json.dumps(context, ensure_ascii=False)}]}
        ]
        try:
            for turn in range(max_tool_turns + 1):
                use_tools = bool(tool_declarations and tool_executor and turn < max_tool_turns)
                resp = _call_native_gemini_once(
                    api_key=api_key,
                    model_id=model_id,
                    contents=contents,
                    tool_declarations=tool_declarations if use_tools else None,
                    expect_json=(not use_tools),
                )
                cand = (resp.get("candidates") or [{}])[0]
                content = cand.get("content") or {}
                parts = content.get("parts") or []
                fn_calls = [p["functionCall"] for p in parts if "functionCall" in p]

                if fn_calls and tool_executor and turn < max_tool_turns:
                    contents.append({"role": "model", "parts": parts})
                    resp_parts = []
                    for fc in fn_calls:
                        fname = fc.get("name", "")
                        fargs = fc.get("args") or {}
                        tool_calls_made.append(fname)
                        result = tool_executor(fname, fargs)
                        resp_parts.append({"functionResponse": {"name": fname, "response": {"result": result}}})
                    contents.append({"role": "user", "parts": resp_parts})
                    continue

                text_out = "\n".join(p.get("text", "") for p in parts if "text" in p)
                parsed = _extract_json(text_out)
                latency = round(time.monotonic() - t0, 2)
                if parsed:
                    usage = resp.get("usageMetadata") or {}
                    return _normalize_thought(parsed), {
                        "rail": "google_gemini_native_api",
                        "model": model_id,
                        "status": "active_gemini",
                        "latency_sec": latency,
                        "tool_calls": tool_calls_made,
                        "usage_tokens": usage.get("totalTokenCount"),
                        "x402_usdc_spent": 0.0,
                        "attempts": attempts,
                    }
                attempts.append({"rail": "native", "model": model_id, "error": f"unparseable(len={len(text_out)})", "latency_sec": latency})
                break
        except urllib.error.HTTPError as e:
            err_body = e.read(240).decode("utf-8", errors="replace")
            attempts.append({"rail": "native", "model": model_id, "error": f"HTTP {e.code}: {err_body[:120]}", "latency_sec": round(time.monotonic() - t0, 2)})
        except Exception as e:
            attempts.append({"rail": "native", "model": model_id, "error": f"{type(e).__name__}: {str(e)[:120]}", "latency_sec": round(time.monotonic() - t0, 2)})

    return None, {"rail": "google_gemini_native_api", "status": "failed", "attempts": attempts}


# ---------------------------------------------------------------------------
# Rail 2: Self-Funded Autonomous x402 Base USDC -> Official Google Gemini
# ---------------------------------------------------------------------------

def run_x402_base_usdc_gemini(
    priv_bytes: bytes,
    base_usdc_balance: float,
    context: Dict[str, Any],
    runway_days: float,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Autonomously pay for official Google Gemini via BlockRun x402 v2 Base USDC (gasless EIP-3009)."""
    if base_usdc_balance < MIN_X402_USDC_BALANCE:
        return None, {
            "rail": "x402_base_usdc_gemini",
            "status": "insufficient_base_usdc",
            "base_usdc_balance": base_usdc_balance,
            "min_required_usdc": MIN_X402_USDC_BALANCE,
        }

    tier = "normal" if runway_days >= 30 else ("conserve" if runway_days >= 7 else "survival")
    model_id = assert_gemini_only_model(X402_GEMINI_MODELS[tier])
    t0 = time.monotonic()

    body = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": GEMINI_SYSTEM_INSTRUCTION},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        "max_tokens": 1800,
        "temperature": 0.2,
    }
    body_bytes = json.dumps(body).encode("utf-8")

    # Step 1: Request x402 HTTP 402 quote from BlockRun
    req1 = urllib.request.Request(
        BLOCKRUN_X402_URL,
        data=body_bytes,
        headers={"Content-Type": "application/json", "User-Agent": "TAN-Gemini-Core/4.0"},
        method="POST",
    )
    quote_obj: Optional[Dict[str, Any]] = None
    try:
        with urllib.request.urlopen(req1, timeout=20) as r:
            # Unexpected 200 without payment
            data = json.loads(r.read().decode("utf-8"))
            parsed = _extract_json(data["choices"][0]["message"].get("content") or "")
            if parsed:
                return _normalize_thought(parsed), {
                    "rail": "x402_base_usdc_gemini",
                    "model": model_id,
                    "status": "active_gemini",
                    "latency_sec": round(time.monotonic() - t0, 2),
                    "x402_usdc_spent": 0.0,
                }
            return None, {"rail": "x402_base_usdc_gemini", "status": "unexpected_200_unparseable"}
    except urllib.error.HTTPError as e:
        if e.code != 402:
            return None, {"rail": "x402_base_usdc_gemini", "status": f"quote_http_{e.code}", "error": e.read(200).decode("utf-8", errors="replace")}
        hdr_b64 = e.headers.get("payment-required") or e.headers.get("X-Payment-Required")
        raw_body = e.read().decode("utf-8", errors="replace")
        if hdr_b64:
            quote_obj = json.loads(base64.b64decode(hdr_b64).decode("utf-8"))
        else:
            quote_obj = json.loads(raw_body)
    except Exception as e:
        return None, {"rail": "x402_base_usdc_gemini", "status": "quote_error", "error": str(e)[:140]}

    # Step 2: Validate exact Base Mainnet USDC option & enforce hard per-call spending cap
    exact_opt = None
    for opt in (quote_obj or {}).get("accepts", []):
        if (
            opt.get("scheme") == "exact"
            and opt.get("network") == "eip155:8453"
            and (opt.get("asset") or "").lower() == evm_crypto.BASE_USDC_ADDRESS.lower()
        ):
            exact_opt = opt
            break
    if not exact_opt:
        return None, {"rail": "x402_base_usdc_gemini", "status": "no_exact_base_usdc_option"}

    amount_micro = int(exact_opt["amount"])
    amount_usdc = amount_micro / 1_000_000.0
    if amount_usdc > MAX_X402_USDC_PER_CALL or amount_usdc > base_usdc_balance:
        return None, {
            "rail": "x402_base_usdc_gemini",
            "status": "spend_cap_or_balance_blocked",
            "quoted_usdc": amount_usdc,
            "cap_usdc": MAX_X402_USDC_PER_CALL,
            "balance_usdc": base_usdc_balance,
        }

    # Step 3: Locally sign gasless EIP-3009 Base USDC TransferWithAuthorization and retry with PAYMENT-SIGNATURE
    payment_sig_b64, _ = evm_crypto.build_x402_v2_base_usdc_header(
        priv_bytes=priv_bytes,
        pay_to=exact_opt["payTo"],
        amount_micro_usdc=str(amount_micro),
        resource_url=BLOCKRUN_X402_URL,
        max_timeout_seconds=int(exact_opt.get("maxTimeoutSeconds", 300)),
        extensions=(quote_obj or {}).get("extensions"),
    )
    req2 = urllib.request.Request(
        BLOCKRUN_X402_URL,
        data=body_bytes,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "TAN-Gemini-Core/4.0",
            "PAYMENT-SIGNATURE": payment_sig_b64,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req2, timeout=45) as r:
            res = json.loads(r.read().decode("utf-8"))
        content = res["choices"][0]["message"].get("content") or ""
        parsed = _extract_json(content)
        latency = round(time.monotonic() - t0, 2)
        if parsed:
            return _normalize_thought(parsed), {
                "rail": "x402_base_usdc_gemini",
                "model": res.get("model") or model_id,
                "status": "active_gemini",
                "latency_sec": latency,
                "x402_usdc_spent": amount_usdc,
            }
        return None, {"rail": "x402_base_usdc_gemini", "status": "paid_but_unparseable", "x402_usdc_spent": amount_usdc}
    except urllib.error.HTTPError as e:
        return None, {
            "rail": "x402_base_usdc_gemini",
            "status": f"payment_http_{e.code}",
            "error": e.read(240).decode("utf-8", errors="replace"),
        }
    except Exception as e:
        return None, {"rail": "x402_base_usdc_gemini", "status": "payment_error", "error": str(e)[:140]}


# ---------------------------------------------------------------------------
# Unified Strict-Gemini Entry Point
# ---------------------------------------------------------------------------

def wake_gemini(
    context: Dict[str, Any],
    base_usdc_balance: float,
    runway_days: float,
    tool_declarations: Optional[List[Dict[str, Any]]] = None,
    tool_executor: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Attempt strictly Gemini-only cognition (Rail 1 Native API -> Rail 2 x402 Base USDC -> Honest Standby)."""
    diagnostics: List[Dict[str, Any]] = []

    # Rail 1: Official Google Gemini Native API (Free Tier 250 RPD or Paid Billing)
    api_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if api_key:
        thought, meta = run_native_gemini_agent(
            api_key=api_key,
            context=context,
            tool_declarations=tool_declarations,
            tool_executor=tool_executor,
        )
        if thought is not None:
            return thought, meta
        diagnostics.append(meta)
    else:
        diagnostics.append({"rail": "google_gemini_native_api", "status": "no_gemini_api_key"})

    # Rail 2: Autonomous Self-Funded Base USDC x402 -> Official Google Gemini
    priv_bytes = evm_crypto.load_private_key_bytes()
    if priv_bytes is not None and base_usdc_balance >= MIN_X402_USDC_BALANCE:
        thought, meta = run_x402_base_usdc_gemini(
            priv_bytes=priv_bytes,
            base_usdc_balance=base_usdc_balance,
            context=context,
            runway_days=runway_days,
        )
        if thought is not None:
            return thought, meta
        diagnostics.append(meta)
    else:
        diagnostics.append(
            {
                "rail": "x402_base_usdc_gemini",
                "status": "awaiting_base_usdc_or_signer",
                "has_signer": priv_bytes is not None,
                "base_usdc_balance": base_usdc_balance,
                "min_required_usdc": MIN_X402_USDC_BALANCE,
            }
        )

    # Rail 3: HONEST STANDBY — NEVER substitute a non-Gemini model!
    return None, {
        "rail": "none",
        "model": "none",
        "status": "awaiting_gemini_fuel",
        "strict_gemini_only": True,
        "x402_usdc_spent": 0.0,
        "note": (
            "No non-Gemini model was substituted. Gemini cognition will activate automatically "
            "on the next 30-min cycle as soon as GEMINI_API_KEY (Google AI Studio free tier: 250 RPD) "
            "is configured OR >= $0.002 USDC arrives in Base treasury 0x63cee161084df40f527EFBFDE3e7bC242F4b32FD."
        ),
        "diagnostics": diagnostics,
    }
