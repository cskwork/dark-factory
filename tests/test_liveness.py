"""Owner-liveness and recovery checks use injected, offline launcher commands."""
from contextlib import redirect_stdout
import io
import json
import os
import subprocess
import time
from unittest.mock import patch

from factory.runtime import (agents, archive_finished, blocked_keys, guard, owns,
                             reconcile_reasons, reconcile_run, recover_run, wake, worker)
from support import Fixture


class LivenessTests(Fixture):
    def setUp(self):
        super().setUp()
        self.data["liveness"] = {"enabled": True}
        self.data["launcher"].update({
            "list_cmd": ["injected", "list", "{token}", "{key}"],
            "inspect_cmd": ["injected", "inspect", "{id}"],
            "archive_cmd": ["injected", "archive", "{id}"],
            "logs_cmd": ["injected", "logs", "{id}"],
            "run_label_args": ["--label", "run={token}"],
            "key_label_args": ["--label", "key={key}"]})
        self.rows = []
        self.owner = {"Status": "idle", "Archived": False}
        self.operations = []
        self.fail = False
        def run(command, **values):
            argv = self.cfg.command(command, **values)
            self.operations.append(argv)
            if self.fail:
                raise OSError("offline query failure")
            payload = self.rows if argv[1] == "list" else self.owner if argv[1] == "inspect" else "saved log"
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload) if not isinstance(payload, str) else payload, "")
        self.adapter = patch.object(self.cfg, "run", side_effect=run)
        self.adapter.start()
        self.addCleanup(self.adapter.stop)

    def running(self, **changes):
        return self.write("running.json", {"token": "old", "started_at": time.time() - 60,
                                           "orchestratorId": "owner", **changes})

    def marker(self, token="old", name="lost"):
        path = self.state / "runs" / token / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_release_requires_token_and_only_matching_owner_releases(self):
        self.running()
        self.assertIn("requires token", guard(self.cfg, release=True)[0])
        self.assertIn("mismatch", guard(self.cfg, release=True, token="new")[0])
        self.assertTrue(owns(self.cfg, "old"))
        self.assertFalse(owns(self.cfg, "new"))
        guard(self.cfg, release=True, token="old")
        self.assertFalse((self.state / "running.json").exists())
        self.assertTrue((self.state / "runs/old/ended").exists())

    def test_release_by_running_owner_marks_ended(self):
        self.running()
        self.rows = [{"id": "owner", "status": "running"}, {"id": "worker", "status": "idle", "key": "PROJ-123"}]
        guard(self.cfg, release=True, token="old")
        self.assertTrue((self.state / "runs/old/ended").exists())
        self.assertFalse((self.state / "runs/old/lost").exists())

    def test_release_with_running_worker_marks_lost(self):
        self.running()
        self.rows = [{"id": "worker", "status": "running", "key": "PROJ-123"}]
        guard(self.cfg, release=True, token="old")
        self.assertTrue((self.state / "runs/old/lost").exists())
        self.assertFalse((self.state / "runs/old/ended").exists())

    def test_failed_release_query_keeps_recovery_marker(self):
        self.running()
        self.fail = True
        guard(self.cfg, release=True, token="old")
        self.assertTrue((self.state / "runs/old/lost").exists())

    def test_archived_or_closed_owner_reclaimed_immediately(self):
        for owner in ({"Status": "idle", "Archived": True}, {"Status": "closed", "Archived": False}):
            with self.subTest(owner=owner):
                self.running()
                self.owner = owner
                _, run = guard(self.cfg, manual=True)
                self.assertIsNotNone(run)
                self.assertNotEqual(run["token"], "old")
                self.assertTrue((self.state / "runs/old/lost").exists())
                self.assertIn("infra_fail", (self.state / "lessons.jsonl").read_text())

    def test_running_worker_prevents_reclaim_even_for_closed_owner(self):
        self.running(noLiveSince=1)
        self.rows = [{"id": "worker", "status": "running"}]
        self.owner = {"Status": "closed", "Archived": True}
        self.assertIsNone(guard(self.cfg, manual=True)[1])
        self.assertNotIn("noLiveSince", self.read("running.json"))
        self.assertFalse(any(op[1] == "inspect" for op in self.operations))

    def test_idle_25_minute_rule_starts_at_first_no_running_observation(self):
        now = time.time()
        self.running(started_at=now - 20000)
        with patch("factory.runtime.time.time", return_value=now):
            self.assertIsNone(guard(self.cfg, manual=True)[1])
        self.assertEqual(self.read("running.json")["noLiveSince"], now)
        with patch("factory.runtime.time.time", return_value=now + 1499):
            self.assertIsNone(guard(self.cfg, manual=True)[1])
        with patch("factory.runtime.time.time", return_value=now + 1500):
            self.assertIsNotNone(guard(self.cfg, manual=True)[1])

    def test_owner_running_resets_idle_clock(self):
        self.running(noLiveSince=1)
        self.owner["Status"] = "running"
        self.assertIsNone(guard(self.cfg, manual=True)[1])
        self.assertNotIn("noLiveSince", self.read("running.json"))

    def test_missing_owner_gets_ten_minute_launch_grace(self):
        now = time.time()
        self.running(started_at=now, orchestratorId=None)
        with patch("factory.runtime.time.time", return_value=now + 599):
            self.assertIsNone(guard(self.cfg, manual=True)[1])
        with patch("factory.runtime.time.time", return_value=now + 600):
            self.assertIsNotNone(guard(self.cfg, manual=True)[1])

    def test_three_query_failures_keep_lock_and_alert_once(self):
        self.running()
        self.fail = True
        for _ in range(4):
            self.assertIsNone(guard(self.cfg, manual=True)[1])
        data = self.read("running.json")
        self.assertEqual(data["queryFailures"], 4)
        entries = [json.loads(line) for line in (self.state / "runs.jsonl").read_text().splitlines()]
        self.assertEqual(sum(row["action"] == "liveness-alert" for row in entries), 1)

    def test_success_resets_consecutive_failure_counter(self):
        self.running(queryFailures=2)
        guard(self.cfg, manual=True)
        self.assertEqual(self.read("running.json")["queryFailures"], 0)
        self.assertNotIn("livenessAlertAt", self.read("running.json"))

    def test_six_hour_live_owner_alert_does_not_reclaim(self):
        self.running(started_at=time.time() - 21601)
        self.rows = [{"id": "owner", "status": "running"}]
        self.assertIsNone(guard(self.cfg, manual=True)[1])
        self.assertIn("livenessAlertAt", self.read("running.json"))

    def test_invalid_inspect_is_query_failure_not_reclaim(self):
        self.running()
        self.owner = {"Status": "closed", "Archived": "false"}
        self.assertIsNone(guard(self.cfg, manual=True)[1])
        self.assertEqual(self.read("running.json")["queryFailures"], 1)

    def test_lost_running_does_not_wake_but_blocks_key(self):
        self.marker()
        self.rows = [{"id": "worker", "status": "running", "labels": {"run": "old", "key": "PROJ-123"}}]
        self.assertEqual(reconcile_reasons(self.cfg), [])
        self.assertIn("PROJ-123", blocked_keys(self.cfg, "new"))

    def test_lost_idle_wakes_and_reconcile_needs_owner_and_second_running_check(self):
        self.marker()
        self.assertEqual(reconcile_reasons(self.cfg), ["reconcile:old"])
        self.running(token="current")
        self.assertFalse(reconcile_run(self.cfg, "old", "wrong"))
        self.rows = [{"id": "worker", "status": "running"}]
        self.assertFalse(reconcile_run(self.cfg, "old", "current"))
        self.rows = []
        self.assertTrue(reconcile_run(self.cfg, "old", "current"))
        self.assertEqual(reconcile_reasons(self.cfg), [])
        self.assertEqual(blocked_keys(self.cfg, "current"), set())

    def test_key_blocking_ignores_archived_agents_and_current_run(self):
        self.marker()
        self.rows = [{"id": "archived", "status": "idle", "archived": True, "key": "PROJ-123"},
                     {"id": "idle", "status": "idle", "key": "PROJ-124"}]
        self.assertEqual(blocked_keys(self.cfg, "new"), {"PROJ-124"})
        self.assertEqual(blocked_keys(self.cfg, "old"), set())
        self.fail = True
        self.assertEqual(blocked_keys(self.cfg, "new"), {"*"})

    def test_recovery_saves_logs_without_marking_ended(self):
        self.marker()
        self.running(token="current")
        self.rows = [{"id": "worker", "status": "idle", "key": "PROJ-123"}]
        result = recover_run(self.cfg, "old", "current")
        self.assertFalse(result["reconciled"])
        self.assertEqual((self.state / "runs/old/worker.recovered.txt").read_text(), "saved log")
        self.assertFalse((self.state / "runs/old/ended").exists())

    def test_archive_requires_local_ended_even_when_liveness_disabled(self):
        self.data["liveness"]["enabled"] = False
        self.marker(name="lost")
        self.rows = [{"id": "idle", "status": "idle"}]
        self.assertEqual(archive_finished(self.cfg), (0, 0))
        self.assertEqual(self.operations, [])
        self.marker(name="ended")
        self.rows += [{"id": "busy", "status": "running"}, {"id": "done", "status": "closed"},
                      {"id": "already", "status": "idle", "archived": True}]
        self.assertEqual(archive_finished(self.cfg), (2, 0))
        self.assertEqual([row[-1] for row in self.operations if row[1] == "archive"], ["idle", "done"])

    def test_switch_off_restores_epoch_and_tokenless_release_and_no_key_checks(self):
        self.running(started_at=time.time() - 11000)
        (self.state / "liveness").write_text("off\n")
        self.assertIsNotNone(guard(self.cfg, manual=True)[1])
        self.assertTrue(owns(self.cfg, "unrelated"))
        self.assertEqual(blocked_keys(self.cfg), set())
        self.assertIn("released", guard(self.cfg, release=True)[0])
        self.assertFalse((self.state / "running").exists())

    def test_legacy_epoch_without_json_can_release_without_token(self):
        (self.state / "running").write_text(str(time.time()))
        self.assertIsNone(guard(self.cfg, manual=True)[1])
        self.assertIn("released", guard(self.cfg, release=True)[0])

    def test_json_field_mapping_and_status_mapping(self):
        self.data["launcher"]["fields"] = {"id": "agent.identity", "status": "phase", "archived": "saved", "labels": "tags"}
        self.data["launcher"]["status_map"] = {"active": "running"}
        self.rows = [{"agent": {"identity": "mapped"}, "phase": "active", "saved": False, "tags": ["run=old", "key=PROJ-123"]}]
        self.assertEqual(agents(self.cfg, "old")[0]["key"], "PROJ-123")
        self.assertEqual(agents(self.cfg, "other"), [])

    def test_factory_scoped_list_requires_matching_run_label(self):
        self.data["launcher"]["list_cmd"] = ["injected", "list"]
        self.rows = [{"id": "unlabelled", "status": "idle"},
                     {"id": "other", "status": "idle", "labels": ["run=other"]},
                     {"id": "owned", "status": "idle", "labels": ["run=old"]}]
        self.assertEqual([row["id"] for row in agents(self.cfg, "old")], ["owned"])

    def test_worker_rejects_old_token_before_launch(self):
        self.running(token="current")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(worker(self.cfg, "builder", "title", "/not/read", token="old"), 3)

    def test_worker_detaches_parent_and_writes_own_result_with_labels(self):
        self.running(token="current")
        brief = self.root / "brief.md"
        brief.write_text("unit")
        result = {"status": "done", "summary": "verified", "key": "PROJ-123", "stage": "built", "waiting": None,
                  "artifacts": [], "checks": []}
        response = subprocess.CompletedProcess([], 0, json.dumps(result), "")
        with patch.dict(os.environ, {"PASEO_AGENT_ID": "parent", "PASEO_WORKSPACE_ID": "workspace"}), \
             patch("factory.runtime.subprocess.run", return_value=response) as launch, redirect_stdout(io.StringIO()):
            self.assertEqual(worker(self.cfg, "builder", "title", brief, token="current", key="PROJ-123", unit="build"), 0)
        self.assertNotIn("PASEO_AGENT_ID", launch.call_args.kwargs["env"])
        self.assertIn("run=current", launch.call_args.args[0])
        self.assertIn("key=PROJ-123", launch.call_args.args[0])
        self.assertEqual(self.read("runs/current/PROJ-123-build.json"), result)

    def test_wake_attaches_owner_identifier_atomically(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(wake(self.cfg, manual=True), 0)
        self.assertEqual(self.read("running.json")["orchestratorId"], "run-123")
        self.assertIn("run=" + self.read("running.json")["token"], self.calls()[-1])

    def test_pending_manual_focus_survives_skipped_wake(self):
        self.running()
        self.rows = [{"id": "owner", "status": "running"}]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(wake(self.cfg, manual=True, key="PROJ-123"), 0)
        pending = (self.state / "pending-focus.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(pending[0])["key"], "PROJ-123")
        self.assertEqual(self.calls(), [])

    def test_worker_budget_clips_timeout_and_rejects_only_exhausted_budget(self):
        now = time.time()
        self.running(token="current", started_at=now - 120 * 60)
        brief = self.root / "brief.md"
        brief.write_text("unit")
        result = {"status": "done", "summary": "verified", "key": None, "stage": "built", "waiting": None,
                  "artifacts": [], "checks": []}
        with patch("factory.runtime.time.time", return_value=now), \
             patch("factory.runtime.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(result), "")) as launch, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(worker(self.cfg, "builder", "title", brief, token="current"), 0)
            self.assertEqual(launch.call_args.kwargs["timeout"], 35 * 60)
            self.assertIn("2100s", launch.call_args.args[0])
        self.running(token="current", started_at=now - 136 * 60)
        with patch("factory.runtime.time.time", return_value=now), \
             patch("factory.runtime.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(result), "")) as launch, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(worker(self.cfg, "builder", "title", brief, token="current"), 0)
            self.assertEqual(launch.call_args.kwargs["timeout"], 19 * 60)
        self.running(token="current", started_at=now - 156 * 60)
        with patch("factory.runtime.time.time", return_value=now), redirect_stdout(io.StringIO()):
            self.assertEqual(worker(self.cfg, "builder", "title", brief, token="current"), 3)

    def test_guard_cli_owns_and_release_exit_codes(self):
        self.running()
        self.assertEqual(self.cli("factory-guard", "--owns", "old").returncode, 0)
        self.assertEqual(self.cli("factory-guard", "--owns", "other").returncode, 1)
        self.assertEqual(self.cli("factory-guard", "--release").returncode, 2)
        (self.state / "running.json").write_text("invalid json")
        self.assertEqual(self.cli("factory-guard", "--owns", "old").returncode, 1)
        self.assertEqual(self.cli("factory-guard", "--release", "--force").returncode, 0)

    def test_configured_notification_fallback_records_alert_result(self):
        self.data["notify"] = {"cmd": ["injected", "notify", "{message}"]}
        self.running(queryFailures=2)
        self.owner = {"Status": "unknown", "Archived": False}
        guard(self.cfg, manual=True)
        actions = [json.loads(line) for line in (self.state / "runs.jsonl").read_text().splitlines()]
        self.assertEqual(actions[-1]["notify"], "sent")
        self.assertTrue(any(op[1] == "notify" for op in self.operations))

    def test_legacy_worker_without_lock_does_not_require_token(self):
        brief = self.root / "brief.md"
        brief.write_text("unit")
        result = {"status": "done", "summary": "verified", "key": None, "stage": "built", "waiting": None,
                  "artifacts": [], "checks": []}
        with patch("factory.runtime.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(result), "")) as launch, redirect_stdout(io.StringIO()):
            self.assertEqual(worker(self.cfg, "builder", "title", brief), 0)
        self.assertFalse(any(arg.startswith("key=") for arg in launch.call_args.args[0]))

    def test_missing_list_archive_field_is_resolved_via_inspect(self):
        self.marker()
        self.rows = [{"id": "archived-worker", "status": "idle", "key": "PROJ-123"}]
        self.owner["Archived"] = True
        self.assertEqual(blocked_keys(self.cfg, "current"), set())
        self.marker(name="ended")
        self.assertEqual(archive_finished(self.cfg), (0, 0))
        self.assertTrue(any(op[1] == "inspect" and op[-1] == "archived-worker" for op in self.operations))
        self.assertFalse(any(op[1] == "archive" for op in self.operations))

    def test_missing_archive_inspection_failure_blocks_key(self):
        self.marker()
        self.rows = [{"id": "worker", "status": "idle", "key": "PROJ-123"}]
        self.owner = {"Status": "idle", "Archived": "unknown"}
        self.assertEqual(blocked_keys(self.cfg, "current"), {"*"})

    def test_pending_focus_bypasses_recent_duplicate_window(self):
        from datetime import date
        (self.state / "done-today").write_text(date.today().isoformat())
        (self.state / "pending-focus.jsonl").write_text(json.dumps({"key": "PROJ-123"}) + "\n")
        self.write("last-wake.json", {"at": time.time()})
        self.assertIsNotNone(guard(self.cfg)[1])

    def test_worker_preserves_stderr_and_rejects_wrong_result_key(self):
        from contextlib import redirect_stderr
        self.running(token="current")
        brief = self.root / "brief.md"
        brief.write_text("unit")
        result = {"status": "failed", "summary": "infra rate-limit", "key": "PROJ-124", "stage": "built", "waiting": None,
                  "artifacts": [], "checks": []}
        response = subprocess.CompletedProcess([], 0, json.dumps(result), "429 overloaded\n")
        with patch("factory.runtime.subprocess.run", return_value=response), redirect_stderr(io.StringIO()) as error:
            with self.assertRaisesRegex(ValueError, "key mismatch"):
                worker(self.cfg, "builder", "title", brief, token="current", key="PROJ-123", unit="build")
        self.assertEqual(error.getvalue(), "429 overloaded\n")
        self.assertEqual((self.state / "runs/current/PROJ-123-build.stderr.txt").read_text(), "429 overloaded\n")
