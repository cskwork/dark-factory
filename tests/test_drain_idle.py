"""Deferrals require real worker progress; backoff applies to individual items."""
from datetime import date
import time
from unittest.mock import patch
from factory import idle
from factory.drain import iso
from factory.runtime import guard
from factory.state import BOOKKEEPING, item_digest, patch as patch_state
from support import Fixture, event


class DrainIdleTests(Fixture):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.notifications = []
        notify = patch.object(idle, "notify", side_effect=lambda cfg, text: self.notifications.append(text) or "sent")
        notify.start()
        self.addCleanup(notify.stop)

    def prior_run(self, workers=1, defer_count=1):
        self.write("queue.json", {f"PROJ-{index}": {"waiting": None, "stage": "tobe"} for index in range(4)})
        idle.decide(self.cfg)
        state = self.read("guard-state.json")
        state["at"] = self.now - 120
        self.write("guard-state.json", state)
        self.write("runs/previous/summary.json", {"startedAt": self.now - 60, "workers": workers})
        queue = self.read("queue.json")
        queue["PROJ-0"]["waiting"] = "reporter"
        for index in range(1, 4):
            queue[f"PROJ-{index}"].update(deferredAt=iso(self.now), deferReason="budget", deferCount=defer_count)
        self.write("queue.json", queue)

    def test_four_runnable_one_processed_three_deferred_are_not_stuck(self):
        self.prior_run()
        result, code = idle.decide(self.cfg)
        self.assertEqual(code, 1)
        self.assertEqual([row for row in result["reasons"] if row.startswith("queue-runnable:")],
                         ["queue-runnable:PROJ-1", "queue-runnable:PROJ-2", "queue-runnable:PROJ-3"])
        self.assertEqual(result["stuck"], [])

    def test_zero_workers_cannot_exempt_deferrals(self):
        self.prior_run(workers=0)
        result, code = idle.decide(self.cfg)
        self.assertEqual(code, 0)
        self.assertIn("stuck-runnable:PROJ-1", result["stuck"])
        self.assertEqual(len(self.notifications), 3)

    def test_four_deferrals_are_stuck_and_notify_once_per_item(self):
        self.prior_run(defer_count=4)
        result, code = idle.decide(self.cfg)
        self.assertEqual(code, 0)
        self.assertIn("stuck-runnable:PROJ-1", result["stuck"])
        idle.decide(self.cfg)
        self.assertEqual(len(self.notifications), 3)

    def test_old_deferral_is_not_exempt(self):
        self.prior_run()
        queue = self.read("queue.json")
        for row in queue.values():
            row["deferredAt"] = "2020-01-01T00:00:00Z"
        self.write("queue.json", queue)
        result, code = idle.decide(self.cfg)
        self.assertEqual(code, 0)
        self.assertIn("stuck-runnable:PROJ-1", result["stuck"])

    def test_missing_summary_or_newer_unrecorded_run_cannot_exempt(self):
        self.prior_run()
        (self.state / "runs/previous/summary.json").unlink()
        self.assertEqual(idle.decide(self.cfg)[1], 0)
        self.prior_run()
        (self.state / "runs/newest").mkdir()
        self.assertEqual(idle.decide(self.cfg)[1], 0)

    def test_bookkeeping_changes_preserve_stuck_hash(self):
        self.prior_run(workers=0)
        queue = self.read("queue.json")
        row = queue["PROJ-1"]
        original = item_digest(row)
        for field in BOOKKEEPING - {"backoffUntil", "backoffLevel", "deferCount"}:
            row[field] = "2025-01-01T00:00:00Z"
        row.update(backoffUntil="2025-01-01T00:00:00Z", backoffLevel=2)
        self.assertEqual(item_digest(row), original)
        self.write("queue.json", queue)
        self.assertIn("stuck-runnable:PROJ-1", idle.decide(self.cfg)[0]["stuck"])

    def test_actual_change_resets_defer_count(self):
        self.prior_run(defer_count=4)
        queue = self.read("queue.json")
        queue["PROJ-1"]["stage"] = "deploy"
        self.write("queue.json", queue)
        result, code = idle.decide(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("queue-runnable:PROJ-1", result["reasons"])
        self.assertEqual(self.read("queue.json")["PROJ-1"]["deferCount"], 0)

    def test_backoff_skips_one_item_and_new_event_still_wakes(self):
        self.write("queue.json", {"PROJ-1": {"waiting": None, "backoffUntil": iso(self.now + 3600)},
                                  "PROJ-2": {"waiting": None}})
        result, code = idle.decide(self.cfg)
        self.assertEqual(code, 1)
        self.assertNotIn("queue-runnable:PROJ-1", result["reasons"])
        self.assertIn("queue-runnable:PROJ-2", result["reasons"])
        self.scan([event(key="PROJ-1")])
        result, code = idle.decide(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("new-events", result["reasons"])

    def test_pending_focus_wakes_even_when_all_items_back_off(self):
        self.write("queue.json", {"PROJ-1": {"waiting": None, "backoffUntil": iso(self.now + 3600)}})
        self.assertEqual(idle.decide(self.cfg)[1], 0)
        (self.state / "pending-focus.jsonl").write_text('{"key":"PROJ-1"}\n')
        result, code = idle.decide(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("pending-focus", result["reasons"])

    def test_pending_focus_survives_scan_failure(self):
        (self.state / "pending-focus.jsonl").write_text('{"key":"PROJ-1"}\n')
        self.scan(code=1)
        with patch("factory.idle.time.sleep"):
            result, code = idle.decide(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("pending-focus", result["reasons"])
        self.assertIn("scan-failed", result["reasons"])

    def test_all_blocked_pending_focus_is_report_only_and_preserved(self):
        pending = self.state / "pending-focus.jsonl"
        snapshot = '{"key":"PROJ-1"}\n{"focus":"PROJ-2"}\n'
        pending.write_text(snapshot)
        with patch("factory.runtime.blocked_keys", return_value={"PROJ-1", "PROJ-2"}):
            result, code = idle.decide(self.cfg)
        self.assertEqual(code, 0)
        self.assertNotIn("pending-focus", result["reasons"])
        self.assertEqual(result["blockedPendingKeys"], ["PROJ-1", "PROJ-2"])
        self.assertEqual(self.read("guard-last.json")["blockedPendingKeys"], ["PROJ-1", "PROJ-2"])
        self.assertEqual(pending.read_text(), snapshot)

    def test_wildcard_block_reports_actual_pending_keys_without_waking(self):
        (self.state / "pending-focus.jsonl").write_text('{"key":"PROJ-1"}\n')
        with patch("factory.runtime.blocked_keys", return_value={"*"}):
            result, code = idle.decide(self.cfg)
        self.assertEqual(code, 0)
        self.assertEqual(result["blockedKeys"], ["*"])
        self.assertEqual(result["blockedPendingKeys"], ["PROJ-1"])

    def test_one_unblocked_pending_key_still_wakes(self):
        (self.state / "pending-focus.jsonl").write_text('{"key":"PROJ-1"}\n{"key":"PROJ-2"}\n')
        with patch("factory.runtime.blocked_keys", return_value={"PROJ-1"}):
            result, code = idle.decide(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("pending-focus", result["reasons"])
        self.assertEqual(result["blockedPendingKeys"], ["PROJ-1"])

    def test_unscoped_request_still_wakes_alongside_blocked_pending_key(self):
        (self.state / "pending-focus.jsonl").write_text('{"key":"PROJ-1"}\n{}\n')
        with patch("factory.runtime.blocked_keys", return_value={"*"}):
            result, code = idle.decide(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("pending-focus", result["reasons"])
        self.assertEqual(result["blockedPendingKeys"], ["PROJ-1"])

    def test_invalid_pending_focus_is_explicit_error(self):
        for content in ('not json\n', '[]\n', '{"key":42}\n'):
            with self.subTest(content=content):
                (self.state / "pending-focus.jsonl").write_text(content)
                result, code = idle.decide(self.cfg)
                self.assertEqual(code, 2)
                self.assertIn("internal-error", result["reasons"])

    def test_tick_guard_reports_blocked_pending_without_launching(self):
        (self.state / "done-today").write_text(date.today().isoformat())
        self.write("last-wake.json", {"at": self.now})
        (self.state / "pending-focus.jsonl").write_text('{"key":"PROJ-1"}\n')
        with patch("factory.runtime.blocked_keys", return_value={"PROJ-1"}):
            message, run = guard(self.cfg)
        self.assertIsNone(run)
        self.assertIn("idle", message)
        self.assertEqual(self.read("guard-last.json")["blockedPendingKeys"], ["PROJ-1"])
        self.assertEqual(self.read("idle.json")["blockedPendingKeys"], ["PROJ-1"])
        self.assertFalse((self.state / "running.json").exists())

    def test_morning_guard_surfaces_blocked_pending_for_report(self):
        (self.state / "pending-focus.jsonl").write_text('{"key":"PROJ-1"}\n')
        with patch("factory.runtime.blocked_keys", return_value={"PROJ-1"}):
            _, run = guard(self.cfg)
        self.assertIsNotNone(run)
        self.assertEqual(run["mode"], "morning")
        self.assertEqual(self.read("guard-last.json")["blockedKeys"], ["PROJ-1"])
        self.assertEqual(self.read("guard-last.json")["blockedPendingKeys"], ["PROJ-1"])

    def test_expired_backoff_runnable_and_prior_hash_preserved_during_backoff(self):
        self.write("queue.json", {"PROJ-1": {"waiting": None, "backoffUntil": iso(self.now - 1)}})
        self.assertIn("queue-runnable:PROJ-1", idle.decide(self.cfg)[0]["reasons"])
        prior = self.read("guard-state.json")["runnable"]["PROJ-1"]
        patch_state(self.state, "queue.json", ["PROJ-1"], {"backoffUntil": iso(self.now + 3600)})
        self.assertEqual(idle.decide(self.cfg)[1], 0)
        self.assertEqual(self.read("guard-state.json")["runnable"]["PROJ-1"], prior)
        patch_state(self.state, "queue.json", ["PROJ-1"], {"backoffUntil": iso(self.now - 1)})
        self.assertIn("stuck-runnable:PROJ-1", idle.decide(self.cfg)[0]["stuck"])

    def test_blocked_keys_skip_runnable_events_and_approval(self):
        self.write("queue.json", {"PROJ-1": {"waiting": None}})
        self.write("approvals.json", {"PROJ-1": {"approved": True}})
        self.scan([event(key="PROJ-1")])
        with patch("factory.runtime.blocked_keys", return_value={"PROJ-1"}):
            result, code = idle.decide(self.cfg)
        self.assertEqual(code, 0)
        self.assertEqual(result["events"], 0)
        self.assertEqual(result["blockedKeys"], ["PROJ-1"])
        with patch("factory.runtime.blocked_keys", return_value={"*"}):
            self.assertEqual(idle.decide(self.cfg)[1], 0)

    def test_serialized_normalization_keeps_concurrent_writer_changes(self):
        self.write("queue.json", {"PROJ-1": {"waiting": "env-human", "stage": "tobe"},
                                  "PROJ-2": {"waiting": "env-down", "needs": "db"}})

        def check(*args, **kwargs):
            patch_state(self.state, "queue.json", ["PROJ-1"], {"stage": "deploy", "waiting": "human-risk"})
            patch_state(self.state, "queue.json", [], {"PROJ-3": {"waiting": "reporter"}})
            patch_state(self.state, "guard-state.json", [], {"unrelated": "preserve"})
            return {}, 10

        with patch("factory.preflight.check", side_effect=check):
            idle.decide(self.cfg)
        queue = self.read("queue.json")
        self.assertEqual(queue["PROJ-1"]["stage"], "deploy")
        self.assertEqual(queue["PROJ-1"]["waiting"], "human-risk")
        self.assertIn("PROJ-3", queue)
        self.assertEqual(self.read("guard-state.json")["unrelated"], "preserve")
