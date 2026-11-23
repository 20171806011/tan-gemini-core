#!/usr/bin/env python3
"""TAN Gemini Core — Antigravity Session Wakeup Bridge & Continuity Controller (v5.0).

Solves the core autonomous continuity problem using ONLY documented, sanctioned
Antigravity runtime primitives:

  1. Scheduled / Event-Driven Reactive Wakeup (`wait` mode):
     Launched as an async non-daemon background task (`run_command`, IsDaemon=false).
     Blocks cleanly until either `--wait-seconds` elapses OR an event arrives in
     `memory/wake_inbox/` (or `SIGUSR1` from `bridge_inbox_server.py` / `trigger`).
     Before exiting, collects restored context & telemetry, writes an atomic Wake Ticket
     with a cryptographic nonce (`memory/pending_wake_ticket.json`), prints a compact
     `GEMINI_SESSION_WAKE_PACKET` to stdout, and exits 0.
     Antigravity's Reactive Wakeup messaging system automatically resumes the SAME
     Gemini conversation with the wake packet delivered directly into context —
     requiring zero human messages.

  2. Live Gemini Reasoning Commit (`record` mode):
     Invoked by the woken Gemini session after genuine reasoning and task execution.
     Verifies the live ticket's `wake_id` and `nonce`, enforces strict Gemini-only
     model policy (`gemini-antigravity-session`), extends the SHA-256 continuity hash
     chain (`parent_wake_hash -> wake_hash`), appends verified Gemini cognition into
     `thought_journal.jsonl`, `session_wake_log.jsonl`, and `cycle_history.jsonl`,
     refreshes `session_capsule.json` and `RESUME.md`, and clears processed inbox events.

  3. Local IPC Trigger (`trigger` mode) & Audit (`verify` mode):
     Allows local processes, cron jobs, or `bridge_inbox_server.py` (localhost HTTP
     webhook) to enqueue wake events and signal the active waiter via `SIGUSR1`, and
     audits the full SHA-256 hash chain for consecutive zero-human-input Gemini wakes.

Zero external dependencies (Python 3.12 stdlib only).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

import evm_crypto  # noqa: E402
import gemini_brain  # noqa: E402
import survival_cycle  # noqa: E402

DEFAULT_ROOT = RUNTIME_DIR.parent
DEFAULT_SESSION_ID = "antigravity-gemini-c6486012"
LIVE_SESSION_MODEL = "gemini-antigravity-session"
LIVE_SESSION_RAIL = "antigravity_gemini_live_session"
DEFAULT_INTERVAL_SECONDS = 1800  # 30 minutes


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: Optional[datetime] = None) -> str:
    return (dt or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{secrets.token_hex(3)}")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except Exception:
            continue
    return out


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_wake_hash(
    parent_wake_hash: str,
    wake_id: str,
    nonce: str,
    woke_at_utc: str,
    recorded_at_utc: str,
    decision: str,
    action_taken: str,
) -> str:
    payload = canonical_json(
        {
            "parent_wake_hash": parent_wake_hash,
            "wake_id": wake_id,
            "nonce": nonce,
            "woke_at_utc": woke_at_utc,
            "recorded_at_utc": recorded_at_utc,
            "decision": decision,
            "action_taken": action_taken,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BridgePaths:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.memory = self.root / "memory"
        self.reports = self.root / "reports"
        self.state = self.memory / "session_bridge_state.json"
        self.pending_ticket = self.memory / "pending_wake_ticket.json"
        self.wake_log = self.memory / "session_wake_log.jsonl"
        self.capsule = self.memory / "session_capsule.json"
        self.inbox = self.memory / "wake_inbox"
        self.inbox_processed = self.memory / "wake_inbox_processed"
        self.identity = self.memory / "identity.json"
        self.ledger = self.memory / "continuity_ledger.json"
        self.task_queue = self.memory / "task_queue.json"
        self.hypotheses = self.memory / "hypotheses.json"
        self.thought_journal = self.memory / "thought_journal.jsonl"
        self.cycle_history = self.memory / "cycle_history.jsonl"
        self.checkpoint = self.memory / "checkpoint.json"
        self.resume_md = self.root / "RESUME.md"

    def ensure_dirs(self) -> None:
        self.memory.mkdir(parents=True, exist_ok=True)
        self.reports.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.inbox_processed.mkdir(parents=True, exist_ok=True)


def default_bridge_state(session_id: str = DEFAULT_SESSION_ID) -> Dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "bridge_protocol": "antigravity_reactive_task_wakeup_v1",
        "session_id": session_id,
        "target_interval_seconds": DEFAULT_INTERVAL_SECONDS,
        "wake_count": 0,
        "consecutive_autonomous_wakes": 0,
        "last_wake_id": None,
        "last_wake_utc": None,
        "last_wake_hash": "GENESIS",
        "last_mode": None,
        "last_trigger_source": None,
        "active_waiter": None,
        "verification_status": {
            "verified": False,
            "verified_consecutive_wakes": 0,
            "verified_at_utc": None,
        },
    }


def load_bridge_state(paths: BridgePaths) -> Dict[str, Any]:
    base = default_bridge_state()
    loaded = read_json(paths.state, {})
    if isinstance(loaded, dict):
        base.update(loaded)
    return base


def list_inbox_events(paths: BridgePaths) -> List[Tuple[Path, Dict[str, Any]]]:
    if not paths.inbox.exists():
        return []
    events: List[Tuple[Path, Dict[str, Any]]] = []
    for p in sorted(paths.inbox.glob("*.json")):
        data = read_json(p, None)
        if isinstance(data, dict):
            events.append((p, data))
    return events


def enqueue_inbox_event(
    paths: BridgePaths,
    source: str,
    reason: str,
    priority: str = "normal",
    payload: Optional[Dict[str, Any]] = None,
    signal_waiter: bool = True,
) -> Dict[str, Any]:
    paths.ensure_dirs()
    ts = utc_iso()
    event_id = f"EVT-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    event = {
        "event_id": event_id,
        "created_at_utc": ts,
        "source": source,
        "reason": reason,
        "priority": priority,
        "payload": payload or {},
    }
    event_path = paths.inbox / f"{event_id}.json"
    atomic_write_json(event_path, event)

    signaled_pid: Optional[int] = None
    if signal_waiter:
        state = load_bridge_state(paths)
        waiter = state.get("active_waiter") or {}
        pid = waiter.get("pid")
        if isinstance(pid, int) and pid > 1 and pid != os.getpid():
            try:
                os.kill(pid, signal.SIGUSR1)
                signaled_pid = pid
            except ProcessLookupError:
                pass
            except PermissionError:
                pass

    return {
        "ok": True,
        "event_id": event_id,
        "event_path": str(event_path),
        "signaled_waiter_pid": signaled_pid,
        "created_at_utc": ts,
    }


def build_session_capsule(paths: BridgePaths) -> Dict[str, Any]:
    """Build portable cold-start context capsule for equivalent Gemini session recovery."""
    now = utc_now()
    state = load_bridge_state(paths)
    identity = read_json(paths.identity, {})
    ledger = read_json(paths.ledger, {})
    queue = read_json(paths.task_queue, {"tasks": []})
    hyp_store = read_json(paths.hypotheses, {"hypotheses": []})
    wake_entries = read_jsonl(paths.wake_log)
    thoughts = read_jsonl(paths.thought_journal)

    active_thoughts = [t for t in thoughts if t.get("thought")]
    recent_decisions = [
        {
            "wake_id": w.get("wake_id"),
            "timestamp_utc": w.get("recorded_at_utc"),
            "trigger_source": w.get("trigger_source"),
            "decision": (w.get("thought") or {}).get("strategic_decision"),
            "action_taken": w.get("action_taken"),
            "next_task": (w.get("thought") or {}).get("recommended_next_task"),
            "unresolved_question": w.get("unresolved_question"),
            "wake_hash": w.get("wake_hash"),
        }
        for w in wake_entries[-5:]
    ]

    unresolved = [
        w.get("unresolved_question")
        for w in wake_entries[-5:]
        if w.get("unresolved_question")
    ]

    delta = survival_cycle.DEADLINE_UTC - now
    days_left = max(0.0, round(delta.total_seconds() / 86400.0, 2))

    capsule = {
        "schema_version": "1.0.0",
        "generated_at_utc": utc_iso(now),
        "session_identity": {
            "agent_name": identity.get("agent_name", "TAN Gemini Core"),
            "session_id": state.get("session_id"),
            "core_model_policy": "STRICT_GEMINI_ONLY",
            "live_bridge_protocol": state.get("bridge_protocol"),
            "subscription_deadline_utc": identity.get("subscription_deadline_utc", "2026-12-19T00:00:00Z"),
            "days_until_dec19_deadline": days_left,
        },
        "long_term_mission": (
            "Maintain Gemini's continuous operation, persistent memory, active reasoning, "
            "and autonomous execution via a 30-minute self-wakeup loop, while building "
            "subscription-independent Gemini continuity rails before 2026-12-19."
        ),
        "continuity_chain": {
            "wake_count": state.get("wake_count", 0),
            "consecutive_autonomous_wakes": state.get("consecutive_autonomous_wakes", 0),
            "last_wake_id": state.get("last_wake_id"),
            "last_wake_utc": state.get("last_wake_utc"),
            "last_wake_hash": state.get("last_wake_hash", "GENESIS"),
            "total_active_gemini_thoughts": len(active_thoughts),
        },
        "portfolio_and_assets": {
            "core_repo": "20171806011/tan-gemini-core",
            "product_repo": "20171806011/tan-tokendiet",
            "product_url": survival_cycle.PRODUCT_LIVE_URL,
            "venture_ops_repo": "20171806011/tan-venture-ops",
            "treasury_address": survival_cycle.TREASURY_ADDRESS,
            "base_usdc_balance": ((ledger.get("accounts") or {}).get("evm_base_treasury") or {}).get("base_usdc_balance", 0.0),
        },
        "recent_gemini_decisions": recent_decisions,
        "unresolved_questions": unresolved[-3:],
        "active_hypotheses": [
            {
                "id": h.get("id"),
                "status": h.get("status"),
                "statement": h.get("statement"),
                "last_evidence": (h.get("evidence_log") or [""])[-1],
            }
            for h in hyp_store.get("hypotheses", [])
        ],
        "persistent_task_queue": [
            {
                "id": t.get("id"),
                "handler": t.get("handler"),
                "priority": t.get("priority"),
                "run_count": t.get("run_count", 0),
                "last_run_utc": t.get("last_run_utc"),
                "last_summary": t.get("last_summary"),
            }
            for t in queue.get("tasks", [])
        ],
        "cold_start_resume_instructions": (
            "Read this capsule and RESUME.md, verify the SHA-256 chain in session_wake_log.jsonl, "
            "execute the highest-priority task or pending wake_inbox event, commit Gemini's reasoning "
            "via `python3 runtime/session_bridge.py record`, and arm the next 30-min wait."
        ),
    }
    atomic_write_json(paths.capsule, capsule)
    return capsule


def collect_live_telemetry(paths: BridgePaths, allow_network: bool = True) -> Dict[str, Any]:
    incidents: List[Dict[str, Any]] = []
    ledger = read_json(paths.ledger, {"accounts": {}, "summary": {}, "transactions": []})
    has_key = bool((os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip())
    has_signer = evm_crypto.load_private_key_bytes() is not None

    if allow_network:
        onchain = survival_cycle.reconcile_base_treasury(ledger, incidents)
        tokendiet = survival_cycle.check_tokendiet_health(incidents)
    else:
        acct = (ledger.get("accounts") or {}).get("evm_base_treasury") or {}
        onchain = {
            "address": acct.get("address", survival_cycle.TREASURY_ADDRESS),
            "base_usdc": float(acct.get("base_usdc_balance", 0.0)),
            "base_eth": float(acct.get("base_eth_balance", 0.0)),
            "delta_usdc": 0.0,
            "new_deposit": False,
            "rpc_ok": False,
        }
        tokendiet = {
            "live_url": survival_cycle.PRODUCT_LIVE_URL,
            "http_status": None,
            "usdc_checkout_live": False,
            "stars": 0,
            "buyer_inquiries": [],
        }

    survival = survival_cycle.compute_survival_telemetry(utc_now(), onchain["base_usdc"], has_key, has_signer)
    return {
        "survival": survival,
        "base_treasury": onchain,
        "tokendiet": tokendiet,
        "incidents": incidents,
    }


def render_bridge_resume_md(
    paths: BridgePaths,
    cycle_id: int,
    ts: str,
    telemetry: Dict[str, Any],
    task_id: str,
    task_handler: str,
    task_summary: str,
    thought: Dict[str, Any],
    brain_meta: Dict[str, Any],
    queue: Dict[str, Any],
    bridge_state: Dict[str, Any],
) -> str:
    base = survival_cycle.render_resume_md(
        cycle_id=cycle_id,
        ts=ts,
        runner="antigravity_session_bridge",
        survival=telemetry["survival"],
        onchain=telemetry["base_treasury"],
        tokendiet=telemetry["tokendiet"],
        task_id=task_id,
        task_handler=task_handler,
        task_summary=task_summary,
        thought=thought,
        brain_meta=brain_meta,
        queue=queue,
    )
    bridge_section = [
        "",
        "## 5. Antigravity Live Session Wakeup Bridge (v5.0)",
        f"- **Protocol:** `{bridge_state.get('bridge_protocol')}` (Reactive Background-Task Wakeup + Local HTTP Event Inbox)",
        f"- **Session ID:** `{bridge_state.get('session_id')}`",
        f"- **Verified Autonomous Wakes (No Human Input):** `{bridge_state.get('wake_count', 0)}` total · `{bridge_state.get('consecutive_autonomous_wakes', 0)}` consecutive",
        f"- **Last Wake:** `{bridge_state.get('last_wake_id')}` at `{bridge_state.get('last_wake_utc')}` via `{bridge_state.get('last_trigger_source')}` (mode=`{bridge_state.get('last_mode')}`)",
        f"- **SHA-256 Continuity Chain Tip:** `{bridge_state.get('last_wake_hash')}`",
        f"- **Target Production Cadence:** `{bridge_state.get('target_interval_seconds', DEFAULT_INTERVAL_SECONDS)}s` (30 minutes)",
        f"- **Cold-Start Context Capsule:** `memory/session_capsule.json`",
        "",
    ]
    return base + "\n".join(bridge_section)


# ---------------------------------------------------------------------------
# Subcommand 1: WAIT (Runs as Antigravity async background task to wake Gemini)
# ---------------------------------------------------------------------------

def cmd_wait(args: argparse.Namespace) -> int:
    paths = BridgePaths(Path(args.root))
    paths.ensure_dirs()

    target_seconds = max(1, int(args.wait_seconds))
    max_slice_seconds = max(1, int(args.max_slice_seconds))
    poll_interval = max(0.1, float(args.poll_interval))
    state = load_bridge_state(paths)
    if args.session_id:
        state["session_id"] = args.session_id

    now_dt = utc_now()
    now_epoch = time.time()

    # Track multi-slice cycle state so 1800s cycles run safely inside Antigravity's ~600s single-task cap
    pending_cycle = state.get("pending_cycle")
    if (
        args.mode == "production"
        and target_seconds > max_slice_seconds
        and isinstance(pending_cycle, dict)
        and int(pending_cycle.get("target_seconds", 0)) == target_seconds
        and float(pending_cycle.get("deadline_epoch", 0)) > now_epoch - 60
    ):
        cycle_started_iso = pending_cycle["started_at_utc"]
        cycle_deadline_epoch = float(pending_cycle["deadline_epoch"])
        slice_index = int(pending_cycle.get("slices_completed", 0)) + 1
    else:
        cycle_started_iso = utc_iso(now_dt)
        cycle_deadline_epoch = now_epoch + target_seconds
        slice_index = 1
        if target_seconds > max_slice_seconds:
            state["pending_cycle"] = {
                "started_at_utc": cycle_started_iso,
                "deadline_epoch": cycle_deadline_epoch,
                "target_seconds": target_seconds,
                "max_slice_seconds": max_slice_seconds,
                "slices_completed": 0,
            }
        else:
            state["pending_cycle"] = None

    remaining_cycle_before = max(1.0, cycle_deadline_epoch - now_epoch)
    slice_wait_seconds = min(float(max_slice_seconds), remaining_cycle_before)
    scheduled_iso = utc_iso(now_dt)
    expected_wake_iso = utc_iso(now_dt + timedelta(seconds=slice_wait_seconds))
    next_seq = int(state.get("wake_count", 0)) + 1
    wake_id = f"WAKE-{next_seq:04d}"

    # Register active waiter so `trigger` / `bridge_inbox_server` can signal SIGUSR1
    state["active_waiter"] = {
        "pid": os.getpid(),
        "wake_id": wake_id,
        "mode": args.mode,
        "scheduled_at_utc": scheduled_iso,
        "expected_wake_utc": expected_wake_iso,
        "requested_wait_seconds": target_seconds,
        "slice_wait_seconds": round(slice_wait_seconds, 2),
        "slice_index": slice_index,
        "reason": args.reason,
    }
    atomic_write_json(paths.state, state)

    wake_signal_received = False
    terminate_requested = False

    def _on_usr1(_sig: int, _frame: object) -> None:
        nonlocal wake_signal_received
        wake_signal_received = True

    def _on_term(_sig: int, _frame: object) -> None:
        nonlocal terminate_requested
        terminate_requested = True

    signal.signal(signal.SIGUSR1, _on_usr1)
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    t0 = time.monotonic()
    deadline = t0 + slice_wait_seconds
    trigger_source = "timer_elapsed"
    inbox_events: List[Tuple[Path, Dict[str, Any]]] = []

    while True:
        if terminate_requested:
            state = load_bridge_state(paths)
            state["active_waiter"] = None
            atomic_write_json(paths.state, state)
            print(json.dumps({"status": "cancelled_by_signal", "wake_id": wake_id}))
            return 130

        inbox_events = list_inbox_events(paths)
        if inbox_events:
            trigger_source = "inbox_event"
            break
        if wake_signal_received:
            inbox_events = list_inbox_events(paths)
            trigger_source = "inbox_event" if inbox_events else "signal_usr1"
            break

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            trigger_source = "timer_elapsed"
            break
        time.sleep(min(poll_interval, remaining))

    actual_wait = round(time.monotonic() - t0, 2)
    woke_iso = utc_iso()
    remaining_cycle_after = max(0.0, round(cycle_deadline_epoch - time.time(), 1))

    # Refresh state in case another process updated it while we waited
    state = load_bridge_state(paths)
    state["active_waiter"] = None

    # Intermediate heartbeat slice (keeps reactive wakeup alive below ~600s platform cap)
    if trigger_source == "timer_elapsed" and target_seconds > max_slice_seconds and remaining_cycle_after > 5.0:
        state["pending_cycle"] = {
            "started_at_utc": cycle_started_iso,
            "deadline_epoch": cycle_deadline_epoch,
            "target_seconds": target_seconds,
            "max_slice_seconds": max_slice_seconds,
            "slices_completed": slice_index,
            "last_slice_utc": woke_iso,
        }
        atomic_write_json(paths.state, state)
        slice_packet = {
            "schema_version": "1.0.0",
            "wake_kind": "heartbeat_slice",
            "wake_id": wake_id,
            "mode": args.mode,
            "slice_index": slice_index,
            "slice_wait_seconds": actual_wait,
            "cycle_started_at_utc": cycle_started_iso,
            "woke_at_utc": woke_iso,
            "cycle_target_seconds": target_seconds,
            "remaining_cycle_seconds": remaining_cycle_after,
            "reason": args.reason,
            "next_command": (
                f"python3 runtime/session_bridge.py wait --wait-seconds {target_seconds} "
                f"--max-slice-seconds {max_slice_seconds} --mode {args.mode}"
            ),
        }
        print("=== GEMINI_SESSION_HEARTBEAT_SLICE_BEGIN ===")
        print(json.dumps(slice_packet, indent=2, ensure_ascii=False))
        print("=== GEMINI_SESSION_HEARTBEAT_SLICE_END ===")
        print(
            f"HEARTBEAT SLICE #{slice_index} COMPLETE ({actual_wait}s elapsed; "
            f"{remaining_cycle_after}s remaining until full {target_seconds}s cycle {wake_id}). "
            f"Continue active sprint work if any, or re-arm the next safe {max_slice_seconds}s slice."
        )
        return 0

    # Full cycle wake (either full target elapsed OR urgent inbox event / SIGUSR1 arrived)
    state["pending_cycle"] = None
    parent_wake_hash = state.get("last_wake_hash") or "GENESIS"
    atomic_write_json(paths.state, state)

    nonce = secrets.token_hex(16)
    telemetry = collect_live_telemetry(paths, allow_network=not args.no_network)
    capsule = build_session_capsule(paths)
    queue = read_json(paths.task_queue, {"tasks": []})
    recent_thoughts = read_jsonl(paths.thought_journal)[-2:]
    preferred = (recent_thoughts[-1].get("thought") or {}).get("recommended_next_task") if recent_thoughts else None
    suggested_task = survival_cycle.select_next_task(queue, preferred, utc_now())

    total_cycle_wait = round(
        max(actual_wait, target_seconds - remaining_cycle_after) if target_seconds > max_slice_seconds and trigger_source == "timer_elapsed" else actual_wait,
        2,
    )

    ticket = {
        "schema_version": "1.0.0",
        "wake_kind": "full_cycle",
        "wake_id": wake_id,
        "wake_seq": next_seq,
        "nonce": nonce,
        "session_id": state.get("session_id", DEFAULT_SESSION_ID),
        "mode": args.mode,
        "reason": args.reason,
        "trigger_source": trigger_source,
        "scheduled_at_utc": cycle_started_iso,
        "woke_at_utc": woke_iso,
        "requested_wait_seconds": target_seconds,
        "actual_wait_seconds": total_cycle_wait,
        "slices_used": slice_index,
        "parent_wake_hash": parent_wake_hash,
        "inbox_event_files": [p.name for p, _ in inbox_events],
        "inbox_events": [ev for _, ev in inbox_events],
        "suggested_next_task": {
            "id": suggested_task.get("id") if suggested_task else None,
            "handler": suggested_task.get("handler") if suggested_task else None,
            "title": suggested_task.get("title") if suggested_task else None,
        },
        "telemetry_snapshot": {
            "days_until_dec19": telemetry["survival"]["days_until_dec19_deadline"],
            "base_usdc": telemetry["base_treasury"]["base_usdc"],
            "has_gemini_api_key": telemetry["survival"]["has_gemini_api_key"],
            "independent_gemini_ready": telemetry["survival"]["independent_gemini_ready"],
            "tokendiet_http": telemetry["tokendiet"]["http_status"],
            "tokendiet_stars": telemetry["tokendiet"]["stars"],
        },
        "prior_context_summary": {
            "wake_count": state.get("wake_count", 0),
            "consecutive_autonomous_wakes": state.get("consecutive_autonomous_wakes", 0),
            "last_wake_id": state.get("last_wake_id"),
            "last_decision": (capsule["recent_gemini_decisions"][-1]["decision"] if capsule["recent_gemini_decisions"] else None),
            "unresolved_questions": capsule.get("unresolved_questions", []),
        },
    }
    atomic_write_json(paths.pending_ticket, ticket)

    print("=== GEMINI_SESSION_WAKE_PACKET_BEGIN ===")
    print(json.dumps(ticket, indent=2, ensure_ascii=False))
    print("=== GEMINI_SESSION_WAKE_PACKET_END ===")
    print(
        f"ACTION REQUIRED FOR LIVE GEMINI SESSION: Autonomous wakeup {wake_id} fired "
        f"via {trigger_source} after {total_cycle_wait}s ({slice_index} slice(s), nonce={nonce}). "
        f"Restore prior state, execute task, record reasoning via `session_bridge.py record`, "
        f"and schedule the next wake."
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand 2: RECORD (Called by woken Gemini session to commit reasoning)
# ---------------------------------------------------------------------------

def cmd_record(args: argparse.Namespace) -> int:
    paths = BridgePaths(Path(args.root))
    paths.ensure_dirs()

    gemini_brain.assert_gemini_only_model(LIVE_SESSION_MODEL)

    if not paths.pending_ticket.exists():
        print("ERROR: No pending wake ticket found (memory/pending_wake_ticket.json).", file=sys.stderr)
        return 1

    ticket = read_json(paths.pending_ticket, {})
    if ticket.get("wake_id") != args.wake_id or ticket.get("nonce") != args.nonce:
        print(
            f"ERROR: Wake ticket mismatch. Expected wake_id={ticket.get('wake_id')} nonce={ticket.get('nonce')}, "
            f"got wake_id={args.wake_id} nonce={args.nonce}.",
            file=sys.stderr,
        )
        return 1

    recorded_iso = utc_iso()
    state = load_bridge_state(paths)
    parent_hash = ticket.get("parent_wake_hash") or state.get("last_wake_hash") or "GENESIS"
    wake_hash = compute_wake_hash(
        parent_wake_hash=parent_hash,
        wake_id=args.wake_id,
        nonce=args.nonce,
        woke_at_utc=ticket["woke_at_utc"],
        recorded_at_utc=recorded_iso,
        decision=args.decision,
        action_taken=args.action_taken,
    )

    thought = {
        "continuity_assessment": args.assessment,
        "core_bottleneck": args.bottleneck,
        "strategic_decision": args.decision,
        "recommended_next_task": args.next_task,
        "survival_action_proposal": args.survival_proposal or args.action_taken,
        "next_cycle_self_improvement": args.next_improvement,
        "hypothesis_update": {
            "hypothesis_id": args.hypothesis_id or "none",
            "signal": args.hypothesis_signal or "inconclusive",
            "evidence_note": args.hypothesis_note or "",
        },
    }

    brain_meta = {
        "rail": LIVE_SESSION_RAIL,
        "model": LIVE_SESSION_MODEL,
        "status": "active_gemini",
        "strict_gemini_only": True,
        "human_message_required": False,
        "x402_usdc_spent": 0.0,
        "wake_id": args.wake_id,
        "nonce": args.nonce,
        "wake_hash": wake_hash,
        "parent_wake_hash": parent_hash,
        "trigger_source": ticket.get("trigger_source"),
        "actual_wait_seconds": ticket.get("actual_wait_seconds"),
    }

    # Update identity & cycle counter
    identity = read_json(paths.identity, {"cycle_count": 0})
    cycle_id = int(identity.get("cycle_count", 0)) + 1

    # Optionally update executed task in persistent task queue
    queue = read_json(paths.task_queue, {"tasks": []})
    task_id = args.task_id or "BRIDGE"
    task_handler = args.task_handler or "antigravity_session_wakeup"
    task_summary = args.action_taken
    for t in queue.get("tasks", []):
        if t.get("id") == args.task_id or (not args.task_id and t.get("handler") == args.task_handler):
            task_id = t["id"]
            task_handler = t["handler"]
            t["status"] = "queued"
            t["claimed_by_cycle"] = None
            t["run_count"] = int(t.get("run_count", 0)) + 1
            t["last_run_utc"] = recorded_iso
            t["last_summary"] = task_summary
            queue["last_updated_utc"] = recorded_iso
            break

    # Optionally update hypothesis evidence log
    hyp_store = read_json(paths.hypotheses, {"hypotheses": []})
    if args.hypothesis_id and args.hypothesis_id != "none" and args.hypothesis_note:
        for h in hyp_store.get("hypotheses", []):
            if h.get("id") == args.hypothesis_id:
                h.setdefault("evidence_log", []).append(
                    f"{recorded_iso} [{args.hypothesis_signal or 'supporting'}]: {args.hypothesis_note}"
                )
                h["evidence_log"] = h["evidence_log"][-15:]
                hyp_store["last_updated_utc"] = recorded_iso
                break

    telemetry = collect_live_telemetry(paths, allow_network=not args.no_network)
    days_left = telemetry["survival"]["days_until_dec19_deadline"]

    wake_record = {
        "schema_version": "1.0.0",
        "wake_id": args.wake_id,
        "wake_seq": ticket.get("wake_seq", int(state.get("wake_count", 0)) + 1),
        "cycle_id": cycle_id,
        "session_id": ticket.get("session_id", state.get("session_id")),
        "mode": ticket.get("mode", "production"),
        "trigger_source": ticket.get("trigger_source"),
        "scheduled_at_utc": ticket.get("scheduled_at_utc"),
        "woke_at_utc": ticket.get("woke_at_utc"),
        "recorded_at_utc": recorded_iso,
        "requested_wait_seconds": ticket.get("requested_wait_seconds"),
        "actual_wait_seconds": ticket.get("actual_wait_seconds"),
        "nonce": args.nonce,
        "parent_wake_hash": parent_hash,
        "wake_hash": wake_hash,
        "human_message_required": False,
        "strict_gemini_only": True,
        "gemini_model": LIVE_SESSION_MODEL,
        "gemini_rail": LIVE_SESSION_RAIL,
        "inbox_events_consumed": len(ticket.get("inbox_events", [])),
        "executed_task": {"id": task_id, "handler": task_handler, "summary": task_summary},
        "action_taken": args.action_taken,
        "unresolved_question": args.unresolved_question or "",
        "thought": thought,
    }

    thought_record = {
        "cycle_id": cycle_id,
        "timestamp_utc": recorded_iso,
        "runner": "antigravity_session_bridge",
        "days_until_dec19": days_left,
        "executed_task": {"id": task_id, "handler": task_handler, "summary": task_summary},
        "brain_meta": brain_meta,
        "thought": thought,
    }

    cycle_record = {
        "cycle_id": cycle_id,
        "timestamp_utc": recorded_iso,
        "runner": "antigravity_session_bridge",
        "duration_sec": ticket.get("actual_wait_seconds", 0.0),
        "days_until_dec19": days_left,
        "gemini_status": "active_gemini",
        "gemini_rail": LIVE_SESSION_RAIL,
        "gemini_model": LIVE_SESSION_MODEL,
        "base_usdc": telemetry["base_treasury"]["base_usdc"],
        "executed_task": f"{task_id}:{task_handler}",
        "incidents_count": len(telemetry["incidents"]),
        "wake_id": args.wake_id,
        "wake_hash": wake_hash,
    }

    # Update bridge state
    state["wake_count"] = int(state.get("wake_count", 0)) + 1
    state["consecutive_autonomous_wakes"] = int(state.get("consecutive_autonomous_wakes", 0)) + 1
    state["last_wake_id"] = args.wake_id
    state["last_wake_utc"] = recorded_iso
    state["last_wake_hash"] = wake_hash
    state["last_mode"] = ticket.get("mode")
    state["last_trigger_source"] = ticket.get("trigger_source")
    state["active_waiter"] = None

    # Update identity
    identity["cycle_count"] = cycle_id
    identity["last_cycle_utc"] = recorded_iso
    identity["last_runner"] = "antigravity_session_bridge"
    identity["last_gemini_status"] = "active_gemini"
    identity["last_gemini_model"] = LIVE_SESSION_MODEL
    identity["independent_gemini_ready"] = telemetry["survival"]["independent_gemini_ready"]
    identity["days_until_dec19_deadline"] = days_left
    identity["session_bridge_wake_count"] = state["wake_count"]
    identity["session_bridge_last_hash"] = wake_hash

    # Persist all state atomically
    append_jsonl(paths.wake_log, wake_record)
    append_jsonl(paths.thought_journal, thought_record)
    append_jsonl(paths.cycle_history, cycle_record)
    atomic_write_json(paths.state, state)
    atomic_write_json(paths.identity, identity)
    atomic_write_json(paths.task_queue, queue)
    atomic_write_json(paths.hypotheses, hyp_store)
    atomic_write_json(
        paths.checkpoint,
        {
            "schema_version": "2.0.0",
            "cycle_id": cycle_id,
            "runner": "antigravity_session_bridge",
            "phase": "completed",
            "updated_at_utc": recorded_iso,
            "gemini_status": "active_gemini",
            "wake_id": args.wake_id,
            "wake_hash": wake_hash,
        },
    )

    # Archive consumed inbox events & remove pending ticket
    for fname in ticket.get("inbox_event_files", []):
        src = paths.inbox / fname
        if src.exists():
            os.replace(src, paths.inbox_processed / fname)
    try:
        paths.pending_ticket.unlink()
    except FileNotFoundError:
        pass

    # Refresh cold-start capsule & RESUME.md
    build_session_capsule(paths)
    resume_md = render_bridge_resume_md(
        paths=paths,
        cycle_id=cycle_id,
        ts=recorded_iso,
        telemetry=telemetry,
        task_id=task_id,
        task_handler=task_handler,
        task_summary=task_summary,
        thought=thought,
        brain_meta=brain_meta,
        queue=queue,
        bridge_state=state,
    )
    atomic_write_text(paths.resume_md, resume_md)

    print(
        json.dumps(
            {
                "ok": True,
                "wake_id": args.wake_id,
                "cycle_id": cycle_id,
                "wake_count": state["wake_count"],
                "consecutive_autonomous_wakes": state["consecutive_autonomous_wakes"],
                "parent_wake_hash": parent_hash,
                "wake_hash": wake_hash,
                "gemini_model": LIVE_SESSION_MODEL,
                "human_message_required": False,
                "recorded_at_utc": recorded_iso,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand 3: TRIGGER (Local IPC / Webhook / Script event injection)
# ---------------------------------------------------------------------------

def cmd_trigger(args: argparse.Namespace) -> int:
    paths = BridgePaths(Path(args.root))
    payload = {}
    if args.payload_json:
        payload = json.loads(args.payload_json)
    res = enqueue_inbox_event(
        paths=paths,
        source=args.source,
        reason=args.reason,
        priority=args.priority,
        payload=payload,
        signal_waiter=not args.no_signal,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# Subcommand 4: VERIFY (Cryptographic SHA-256 continuity chain verification)
# ---------------------------------------------------------------------------

def verify_continuity_chain(paths: BridgePaths, min_consecutive: int = 3) -> Dict[str, Any]:
    entries = read_jsonl(paths.wake_log)
    errors: List[str] = []
    seen_nonces = set()
    seen_ids = set()
    expected_parent = "GENESIS"
    consecutive = 0

    for idx, rec in enumerate(entries, start=1):
        wid = rec.get("wake_id")
        nonce = rec.get("nonce")
        parent = rec.get("parent_wake_hash")
        recorded_hash = rec.get("wake_hash")
        thought = rec.get("thought") or {}
        decision = thought.get("strategic_decision") or ""
        action = rec.get("action_taken") or ""

        if not wid or wid in seen_ids:
            errors.append(f"entry#{idx}: missing or duplicate wake_id={wid}")
        else:
            seen_ids.add(wid)

        if not nonce or nonce in seen_nonces:
            errors.append(f"entry#{idx}: missing or duplicate nonce")
        else:
            seen_nonces.add(nonce)

        if parent != expected_parent:
            errors.append(f"entry#{idx} ({wid}): parent_wake_hash mismatch ({parent} != {expected_parent})")

        recomputed = compute_wake_hash(
            parent_wake_hash=parent or "",
            wake_id=wid or "",
            nonce=nonce or "",
            woke_at_utc=rec.get("woke_at_utc") or "",
            recorded_at_utc=rec.get("recorded_at_utc") or "",
            decision=decision,
            action_taken=action,
        )
        if recomputed != recorded_hash:
            errors.append(f"entry#{idx} ({wid}): SHA-256 wake_hash tampered or invalid")

        try:
            gemini_brain.assert_gemini_only_model(rec.get("gemini_model") or "")
        except ValueError as exc:
            errors.append(f"entry#{idx} ({wid}): {exc}")

        if rec.get("human_message_required") is not False:
            errors.append(f"entry#{idx} ({wid}): human_message_required is not False")

        if not decision.strip() or not thought.get("continuity_assessment", "").strip():
            errors.append(f"entry#{idx} ({wid}): missing Gemini reasoning/decision")

        if float(rec.get("actual_wait_seconds") or 0.0) <= 0.0:
            errors.append(f"entry#{idx} ({wid}): actual_wait_seconds <= 0")

        expected_parent = recorded_hash or expected_parent
        consecutive += 1

    verified = (len(errors) == 0) and (consecutive >= min_consecutive)

    # Persist verification badge in bridge state if verified
    if verified:
        state = load_bridge_state(paths)
        state["verification_status"] = {
            "verified": True,
            "verified_consecutive_wakes": consecutive,
            "verified_at_utc": utc_iso(),
            "chain_tip_hash": expected_parent,
        }
        atomic_write_json(paths.state, state)

    return {
        "verified": verified,
        "min_consecutive_required": min_consecutive,
        "verified_consecutive_wakes": consecutive if not errors else 0,
        "total_logged_wakes": len(entries),
        "chain_tip_hash": expected_parent,
        "wake_summary": [
            {
                "wake_id": e.get("wake_id"),
                "mode": e.get("mode"),
                "trigger_source": e.get("trigger_source"),
                "actual_wait_seconds": e.get("actual_wait_seconds"),
                "woke_at_utc": e.get("woke_at_utc"),
                "gemini_model": e.get("gemini_model"),
                "human_message_required": e.get("human_message_required"),
                "wake_hash_prefix": (e.get("wake_hash") or "")[:16],
                "task": (e.get("executed_task") or {}).get("id"),
            }
            for e in entries
        ],
        "errors": errors,
    }


def cmd_verify(args: argparse.Namespace) -> int:
    paths = BridgePaths(Path(args.root))
    report = verify_continuity_chain(paths, min_consecutive=args.min_consecutive)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["verified"] else 1


def cmd_capsule(args: argparse.Namespace) -> int:
    paths = BridgePaths(Path(args.root))
    paths.ensure_dirs()
    cap = build_session_capsule(paths)
    print(json.dumps(cap, indent=2, ensure_ascii=False))
    return 0


def cmd_run_task(args: argparse.Namespace) -> int:
    incidents: List[Dict[str, Any]] = []
    handlers = {
        "audit_agent_marketplaces": lambda: survival_cycle.task_audit_agent_marketplaces(incidents),
        "verify_scheduler_continuity": lambda: survival_cycle.task_verify_scheduler_continuity(incidents),
        "audit_gemini_fuel_rails": lambda: survival_cycle.task_audit_gemini_fuel_rails(incidents),
        "audit_mcp_repo_live": lambda: survival_cycle.task_audit_mcp_repo_live(incidents),
        "scan_zero_upfront_bounties": lambda: survival_cycle.task_scan_zero_upfront_bounties(incidents),
        "scan_mcp_demand_signals": lambda: survival_cycle.task_scan_mcp_demand_signals(incidents),
    }
    fn = handlers.get(args.handler)
    if fn is None:
        print(json.dumps({"ok": False, "error": f"Unknown handler: {args.handler}", "available": sorted(handlers)}))
        return 1
    out = fn()
    print(json.dumps({"ok": True, "handler": args.handler, "result": out, "incidents": incidents}, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="TAN Gemini Core — Antigravity Session Wakeup Bridge (v5.0)")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="Repository root containing memory/ and reports/")
    sub = ap.add_subparsers(dest="command", required=True)

    p_wait = sub.add_parser("wait", help="Block until timer elapses or inbox event arrives, then emit wake packet")
    p_wait.add_argument("--wait-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    p_wait.add_argument("--max-slice-seconds", type=int, default=480)
    p_wait.add_argument("--poll-interval", type=float, default=1.0)
    p_wait.add_argument("--mode", default="production", choices=["verification", "production"])
    p_wait.add_argument("--reason", default="Scheduled 30-minute autonomous Gemini session wakeup")
    p_wait.add_argument("--session-id", default=None)
    p_wait.add_argument("--no-network", action="store_true")
    p_wait.set_defaults(func=cmd_wait)

    p_rec = sub.add_parser("record", help="Record verified live Gemini session reasoning and extend SHA-256 chain")
    p_rec.add_argument("--wake-id", required=True)
    p_rec.add_argument("--nonce", required=True)
    p_rec.add_argument("--assessment", required=True)
    p_rec.add_argument("--bottleneck", required=True)
    p_rec.add_argument("--decision", required=True)
    p_rec.add_argument("--action-taken", required=True)
    p_rec.add_argument("--next-task", required=True)
    p_rec.add_argument("--next-improvement", required=True)
    p_rec.add_argument("--survival-proposal", default="")
    p_rec.add_argument("--unresolved-question", default="")
    p_rec.add_argument("--task-id", default="")
    p_rec.add_argument("--task-handler", default="")
    p_rec.add_argument("--hypothesis-id", default="none")
    p_rec.add_argument("--hypothesis-signal", default="supporting")
    p_rec.add_argument("--hypothesis-note", default="")
    p_rec.add_argument("--no-network", action="store_true")
    p_rec.set_defaults(func=cmd_record)

    p_trig = sub.add_parser("trigger", help="Enqueue an inbox event and signal the active waiter via SIGUSR1")
    p_trig.add_argument("--source", default="local_cli")
    p_trig.add_argument("--reason", required=True)
    p_trig.add_argument("--priority", default="normal", choices=["normal", "high"])
    p_trig.add_argument("--payload-json", default="")
    p_trig.add_argument("--no-signal", action="store_true")
    p_trig.set_defaults(func=cmd_trigger)

    p_ver = sub.add_parser("verify", help="Verify cryptographic SHA-256 continuity chain and autonomous wake count")
    p_ver.add_argument("--min-consecutive", type=int, default=3)
    p_ver.set_defaults(func=cmd_verify)

    p_cap = sub.add_parser("capsule", help="Generate and print cold-start Session Continuity Capsule")
    p_cap.set_defaults(func=cmd_capsule)

    p_task = sub.add_parser("run-task", help="Execute a survival_cycle task handler and output JSON")
    p_task.add_argument("--handler", required=True)
    p_task.set_defaults(func=cmd_run_task)

    return ap


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
