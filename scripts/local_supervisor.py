#!/usr/bin/env python3
"""Hardened 30-minute local supervisor for TAN Gemini Core (zero external dependencies)."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path("/home/antigravity/tan-venture/runtime")
LOG_DIR = Path("/home/antigravity/tan-venture/logs")
LOCK_FILE = RUNTIME_DIR / "gemini_core_supervisor.lock"
PID_FILE = RUNTIME_DIR / "gemini_core_supervisor.pid"
HEARTBEAT_FILE = RUNTIME_DIR / "gemini_core_heartbeat.json"

_RUNNING = True


def _stop(_sig: int, _frame: object) -> None:
    global _RUNNING
    _RUNNING = False


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_hb(data: dict) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HEARTBEAT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, HEARTBEAT_FILE)


def main() -> int:
    ap = argparse.ArgumentParser(description="TAN Gemini Core 30-minute local supervisor")
    ap.add_argument("--interval-seconds", type=int, default=1800)
    ap.add_argument("--max-cycles", type=int, default=0)
    ap.add_argument("--force-cycles", action="store_true")
    args = ap.parse_args()

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock_fh = LOCK_FILE.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("tan-gemini-core supervisor already running.", file=sys.stderr)
        return 0

    PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    cycles = 0
    fails = 0
    log_path = LOG_DIR / "gemini_core_supervisor.log"

    while _RUNNING:
        ts = _utc()
        try:
            subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], cwd=str(ROOT), capture_output=True, timeout=25)
        except Exception:
            pass
        cmd = [sys.executable, str(ROOT / "runtime" / "survival_cycle.py"), "--runner", "local_vm"]
        if args.force_cycles:
            cmd.append("--force")
        try:
            proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=180)
            rc, out = proc.returncode, (proc.stdout + proc.stderr).strip()
        except subprocess.TimeoutExpired:
            rc, out = 124, "TIMEOUT"
        except Exception as exc:
            rc, out = 1, str(exc)

        cycles += 1
        fails = 0 if rc == 0 else fails + 1
        with log_path.open("a", encoding="utf-8") as lf:
            lf.write(f"\n=== GEMINI CORE SUPERVISOR #{cycles} @ {ts} (rc={rc}) ===\n{out}\n")

        next_sleep = args.interval_seconds if rc == 0 else min(args.interval_seconds, 20 * (2 ** min(4, fails - 1)))
        _write_hb({"pid": os.getpid(), "status": "healthy" if rc == 0 else "recovering", "last_cycle_utc": ts, "last_rc": rc, "cycles": cycles, "interval_seconds": args.interval_seconds})

        if args.max_cycles > 0 and cycles >= args.max_cycles:
            break
        slept = 0
        while _RUNNING and slept < next_sleep:
            time.sleep(min(5, next_sleep - slept))
            slept += min(5, next_sleep - slept)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
