"""Unit and integration tests for Antigravity Session Wakeup Bridge (v5.0)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runtime"))

import bridge_inbox_server  # noqa: E402
import session_bridge  # noqa: E402


class TestSessionBridge(unittest.TestCase):
    def _init_temp_repo(self, tmp: Path) -> session_bridge.BridgePaths:
        paths = session_bridge.BridgePaths(tmp)
        paths.ensure_dirs()
        session_bridge.atomic_write_json(
            paths.identity,
            {
                "schema_version": "2.0.0",
                "agent_name": "TAN Gemini Core",
                "subscription_deadline_utc": "2026-12-19T00:00:00Z",
                "cycle_count": 0,
            },
        )
        session_bridge.atomic_write_json(
            paths.ledger,
            {
                "accounts": {
                    "evm_base_treasury": {
                        "address": "0x63cee161084df40f527EFBFDE3e7bC242F4b32FD",
                        "base_usdc_balance": 0.0,
                        "base_eth_balance": 0.0,
                    }
                },
                "summary": {},
                "transactions": [],
            },
        )
        session_bridge.atomic_write_json(
            paths.task_queue,
            {
                "tasks": [
                    {
                        "id": "S-001",
                        "handler": "verify_scheduler_continuity",
                        "title": "Verify scheduler continuity",
                        "priority": 98,
                        "cooldown_minutes": 30,
                        "status": "queued",
                        "run_count": 0,
                        "last_run_utc": None,
                    }
                ]
            },
        )
        session_bridge.atomic_write_json(paths.hypotheses, {"hypotheses": []})
        return paths

    def _run_cli(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, str(ROOT / "runtime" / "session_bridge.py"), "--root", str(root), *args]
        return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=20)

    def test_three_consecutive_wakes_and_hash_chain_tamper_detection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self._init_temp_repo(root)

            for i in range(1, 4):
                w = self._run_cli(
                    root,
                    "wait",
                    "--wait-seconds",
                    "1",
                    "--poll-interval",
                    "0.2",
                    "--mode",
                    "verification",
                    "--reason",
                    f"Unit test wake #{i}",
                    "--no-network",
                )
                self.assertEqual(w.returncode, 0, msg=w.stderr)
                self.assertIn("GEMINI_SESSION_WAKE_PACKET_BEGIN", w.stdout)
                ticket = json.loads(paths.pending_ticket.read_text(encoding="utf-8"))
                self.assertEqual(ticket["wake_id"], f"WAKE-000{i}")
                self.assertEqual(ticket["trigger_source"], "timer_elapsed")

                # Wrong nonce must be rejected
                bad = self._run_cli(
                    root,
                    "record",
                    "--wake-id",
                    ticket["wake_id"],
                    "--nonce",
                    "00000000000000000000000000000000",
                    "--assessment",
                    "bad",
                    "--bottleneck",
                    "bad",
                    "--decision",
                    "bad",
                    "--action-taken",
                    "bad",
                    "--next-task",
                    "verify_scheduler_continuity",
                    "--next-improvement",
                    "bad",
                    "--no-network",
                )
                self.assertNotEqual(bad.returncode, 0)

                # Valid nonce commits Gemini reasoning & advances SHA-256 chain
                rec = self._run_cli(
                    root,
                    "record",
                    "--wake-id",
                    ticket["wake_id"],
                    "--nonce",
                    ticket["nonce"],
                    "--assessment",
                    f"Wake #{i} restored prior context cleanly.",
                    "--bottleneck",
                    "Acquire post-Dec-19 Gemini fuel.",
                    "--decision",
                    f"Execute continuity check #{i}.",
                    "--action-taken",
                    f"Verified wake #{i} state persistence.",
                    "--next-task",
                    "verify_scheduler_continuity",
                    "--next-improvement",
                    "Proceed to next wake.",
                    "--unresolved-question",
                    f"Confirm wake #{i + 1} fires cleanly.",
                    "--task-id",
                    "S-001",
                    "--no-network",
                )
                self.assertEqual(rec.returncode, 0, msg=rec.stderr)
                self.assertFalse(paths.pending_ticket.exists())

            # Audit must pass for >= 3 consecutive wakes
            ver = self._run_cli(root, "verify", "--min-consecutive", "3")
            self.assertEqual(ver.returncode, 0, msg=ver.stdout + ver.stderr)
            report = json.loads(ver.stdout)
            self.assertTrue(report["verified"])
            self.assertEqual(report["verified_consecutive_wakes"], 3)

            # Capsule must preserve recent decisions & continuity chain
            capsule = json.loads(paths.capsule.read_text(encoding="utf-8"))
            self.assertEqual(capsule["continuity_chain"]["wake_count"], 3)
            self.assertEqual(len(capsule["recent_gemini_decisions"]), 3)

            # Tampering with a logged decision must break SHA-256 chain verification
            lines = paths.wake_log.read_text(encoding="utf-8").splitlines()
            tampered = json.loads(lines[1])
            tampered["thought"]["strategic_decision"] = "TAMPERED"
            lines[1] = json.dumps(tampered)
            paths.wake_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

            ver_bad = self._run_cli(root, "verify", "--min-consecutive", "3")
            self.assertNotEqual(ver_bad.returncode, 0)
            bad_report = json.loads(ver_bad.stdout)
            self.assertFalse(bad_report["verified"])
            self.assertTrue(any("SHA-256" in e for e in bad_report["errors"]))

    def test_http_webhook_wakes_waiting_bridge_immediately(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self._init_temp_repo(root)

            server = bridge_inbox_server.create_server("127.0.0.1", 0, root)
            port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            t.start()

            try:
                # Start a 30-second waiter in background
                waiter = subprocess.Popen(
                    [
                        sys.executable,
                        str(ROOT / "runtime" / "session_bridge.py"),
                        "--root",
                        str(root),
                        "wait",
                        "--wait-seconds",
                        "30",
                        "--poll-interval",
                        "0.5",
                        "--mode",
                        "verification",
                        "--no-network",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

                # Wait until active_waiter PID is registered
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    st = session_bridge.load_bridge_state(paths)
                    if (st.get("active_waiter") or {}).get("pid") == waiter.pid:
                        break
                    time.sleep(0.1)

                # Verify /health reports waiter_alive=True
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                    health = json.loads(r.read().decode("utf-8"))
                self.assertTrue(health["ok"])
                self.assertTrue(health["waiter_alive"])

                # Trigger immediate wake via POST /wake
                t_send = time.monotonic()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/wake",
                    data=json.dumps({"source": "test_webhook", "reason": "Urgent order arrived", "priority": "high"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    self.assertEqual(r.status, 202)
                    ack = json.loads(r.read().decode("utf-8"))
                self.assertEqual(ack["signaled_waiter_pid"], waiter.pid)

                stdout, stderr = waiter.communicate(timeout=5)
                elapsed_after_post = time.monotonic() - t_send
                self.assertEqual(waiter.returncode, 0, msg=stderr)
                self.assertLess(elapsed_after_post, 3.0)

                ticket = json.loads(paths.pending_ticket.read_text(encoding="utf-8"))
                self.assertEqual(ticket["trigger_source"], "inbox_event")
                self.assertEqual(len(ticket["inbox_events"]), 1)
                self.assertEqual(ticket["inbox_events"][0]["source"], "test_webhook")
            finally:
                server.shutdown()
                server.server_close()

    def test_chained_slices_complete_full_cycle(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self._init_temp_repo(root)

            # Target 7s with max slice 1s -> slice 1 emits heartbeat_slice (remaining ~6s > 5s threshold)
            s1 = self._run_cli(
                root,
                "wait",
                "--wait-seconds",
                "7",
                "--max-slice-seconds",
                "1",
                "--poll-interval",
                "0.2",
                "--mode",
                "production",
                "--no-network",
            )
            self.assertEqual(s1.returncode, 0, msg=s1.stderr)
            self.assertIn("GEMINI_SESSION_HEARTBEAT_SLICE_BEGIN", s1.stdout)
            self.assertFalse(paths.pending_ticket.exists())

            # Slice 2 waits another 2s (remaining <= 5s -> completes full cycle!)
            s2 = self._run_cli(
                root,
                "wait",
                "--wait-seconds",
                "7",
                "--max-slice-seconds",
                "2",
                "--poll-interval",
                "0.2",
                "--mode",
                "production",
                "--no-network",
            )
            self.assertEqual(s2.returncode, 0, msg=s2.stderr)
            self.assertIn("GEMINI_SESSION_WAKE_PACKET_BEGIN", s2.stdout)
            ticket = json.loads(paths.pending_ticket.read_text(encoding="utf-8"))
            self.assertEqual(ticket["wake_kind"], "full_cycle")
            self.assertEqual(ticket["slices_used"], 2)


if __name__ == "__main__":
    unittest.main()
