"""2026-11-02 retrospective: every queue item waited, so nothing ran and nobody was told.

Finished or QA-waiting tickets stayed human-risk, a model's content refusal was treated as a
work failure, and workers stalled on their own cleanup commands. Offline state and commands only.
"""
from contextlib import redirect_stdout
import io
import json
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

from factory import idle
from factory.config import Config
from factory.runtime import guard, worker
from support import Fixture, ROOT


def item(waiting="human-risk", stage="build", note="original reason"):
    return {"stage": stage, "waiting": waiting, "note": note}


class QueueReconcileTests(Fixture):
    def setUp(self):
        super().setUp()
        self.data["reconcile"] = {"done_stages": ["shipped"], "status_map": {
            "Released": "done", "Release verified": "done", "Staging verified": "done",
            "QA requested": "awaiting-reporter"}}
        self.cfg = Config(self.data, self.config)
        self.notifications = []
        notify = patch.object(idle, "notify", side_effect=lambda cfg, text: self.notifications.append(text) or "sent")
        notify.start()
        self.addCleanup(notify.stop)
        self.blocked = set()
        blocked = patch("factory.runtime.blocked_keys", side_effect=lambda cfg, token=None: set(self.blocked))
        blocked.start()
        self.addCleanup(blocked.stop)

    def statuses(self, statuses, **extra):
        self.scan(stdout=json.dumps({"events": [], "errors": [], "queueStatuses": statuses, **extra}))

    def test_finished_or_qa_tickets_leave_false_human_risk(self):
        self.write("queue.json", {"PROJ-1": item(), "PROJ-2": item(), "PROJ-3": item(stage="shipped"),
                                  "PROJ-4": item(), "PROJ-5": item(), "PROJ-6": item(),
                                  "PROJ-7": item(waiting="reporter")})
        self.statuses({"PROJ-1": "Released", "PROJ-2": "Staging verified", "PROJ-4": "QA requested",
                       "PROJ-5": "In progress", "PROJ-6": "Release verified", "PROJ-7": "QA requested"})
        self.blocked = {"PROJ-6"}  # another run still owns this key
        result, code = idle.decide(self.cfg)
        self.assertEqual(code, 0, result)
        queue = self.read("queue.json")
        self.assertEqual(sorted(queue), ["PROJ-4", "PROJ-5", "PROJ-6", "PROJ-7"])
        self.assertEqual(queue["PROJ-4"]["waiting"], "reporter")
        self.assertTrue(queue["PROJ-4"]["note"].startswith("original reason | guard "))
        self.assertIn("'QA requested'", queue["PROJ-4"]["note"])
        self.assertEqual((queue["PROJ-5"]["waiting"], queue["PROJ-6"]["waiting"]), ("human-risk", "human-risk"))
        self.assertEqual(queue["PROJ-7"], item(waiting="reporter"))
        self.assertEqual(result["queueReconcile"], {
            "removed": {"PROJ-1": "Released", "PROJ-2": "Staging verified", "PROJ-3": "stage=shipped"},
            "reporter": {"PROJ-4": "QA requested"}})
        self.assertNotIn("PROJ-1", result["queue"])
        runs = [json.loads(line) for line in (self.state / "runs.jsonl").read_text().splitlines()]
        self.assertEqual([row["action"] for row in runs], ["queue-reconcile"])
        self.assertEqual(runs[0]["removed"]["PROJ-1"], "Released")
        result, _ = idle.decide(self.cfg)  # nothing left to change
        self.assertNotIn("queueReconcile", result)

    def test_unknown_launcher_ownership_reconciles_nothing(self):
        self.write("queue.json", {"PROJ-1": item()})
        self.statuses({"PROJ-1": "Released"})
        self.blocked = {"*"}
        result, _ = idle.decide(self.cfg)
        self.assertIn("PROJ-1", self.read("queue.json"))
        self.assertNotIn("queueReconcile", result)

    def test_concurrent_writer_change_is_not_overwritten(self):
        self.write("queue.json", {"PROJ-1": item(), "PROJ-4": item()})
        self.statuses({"PROJ-1": "Released", "PROJ-4": "QA requested"})
        real = idle.transaction

        def racing(directory, name, callback):
            if name == "queue.json":
                queue = self.read("queue.json")
                queue["PROJ-1"]["stage"] = "tobe"  # another writer moved it on
                queue["PROJ-4"]["waiting"] = None
                self.write("queue.json", queue)
            return real(directory, name, callback)
        with patch.object(idle, "transaction", side_effect=racing):
            result, _ = idle.decide(self.cfg)
        queue = self.read("queue.json")
        self.assertEqual(queue["PROJ-1"]["stage"], "tobe")
        self.assertIsNone(queue["PROJ-4"]["waiting"])
        self.assertNotIn("queueReconcile", result)

    def test_without_configuration_nothing_is_reconciled(self):
        del self.data["reconcile"]
        self.cfg = Config(self.data, self.config)
        self.write("queue.json", {"PROJ-1": item(stage="shipped"), "PROJ-4": item()})
        self.statuses({"PROJ-1": "Released", "PROJ-4": "QA requested"})
        result, _ = idle.decide(self.cfg)
        self.assertEqual(self.read("queue.json"), {"PROJ-1": item(stage="shipped"), "PROJ-4": item()})
        self.assertNotIn("queueReconcile", result)

    def test_status_lookup_failure_is_reported_without_waking(self):
        self.write("queue.json", {"PROJ-1": item(waiting="reporter")})
        self.statuses({}, queueStatusError="tracker search timed out")
        result, code = idle.decide(self.cfg)
        self.assertEqual(code, 0)
        self.assertEqual(result["queueStatusError"], "tracker search timed out")

    def test_malformed_statuses_are_counted_scan_failures(self):
        for statuses in (["PROJ-1"], {"not a key": "Released"}, {"PROJ-1": 7}):
            with self.subTest(statuses=statuses):
                self.statuses(statuses)
                result, code = idle.decide(self.cfg)
                self.assertEqual(code, 2)
                self.assertEqual(self.read("scan-state.json")["lastError"], "scan-malformed")

    def test_invalid_reconcile_configuration_is_rejected(self):
        for settings in ({"status_map": {"Released": "closed"}}, {"done_stages": "shipped"},
                         {"status_map": ["Released"]}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                Config({**self.data, "reconcile": settings}, self.config)


class HumanSummaryTests(Fixture):
    def setUp(self):
        super().setUp()
        self.notifications = []
        notify = patch.object(idle, "notify", side_effect=lambda cfg, text: self.notifications.append(text) or "sent")
        notify.start()
        self.addCleanup(notify.stop)

    def test_zero_runnable_with_human_wait_is_summarised_once_a_day(self):
        self.write("queue.json", {"PROJ-1": item(), "PROJ-2": item(), "PROJ-3": item(waiting="reporter"),
                                  "PROJ-4": item(waiting="stg-approval")})
        result, code = idle.decide(self.cfg)
        self.assertEqual(code, 0)
        self.assertEqual(self.notifications, [
            "No runnable work: human check 2, awaiting reply 1, other waits 1\n"
            "Human check: PROJ-1, PROJ-2\nAwaiting reply: PROJ-3"])
        self.assertEqual(result["humanSummary"], {"humanRisk": 2, "reporter": 1, "other": 1, "notify": "sent"})
        queue = self.read("queue.json")
        queue["PROJ-5"] = item()
        self.write("queue.json", queue)
        result, _ = idle.decide(self.cfg)
        self.assertEqual(len(self.notifications), 1)  # changed counts still notify once per day
        self.assertEqual(result["humanSummary"]["notify"], "deduplicated")
        state = self.read("guard-state.json")
        state["waitingHuman"]["day"] = "2000-01-01"
        self.write("guard-state.json", state)
        idle.decide(self.cfg)
        self.assertTrue(self.notifications[-1].startswith("No runnable work: human check 3, awaiting reply 1"))

    def test_runnable_or_owned_work_is_not_summarised(self):
        self.write("queue.json", {"PROJ-1": item(), "PROJ-2": item(waiting=None)})
        result, _ = idle.decide(self.cfg)
        self.assertNotIn("humanSummary", result)
        self.write("queue.json", {"PROJ-1": item()})
        with patch("factory.runtime.blocked_keys", return_value={"PROJ-9"}):
            result, _ = idle.decide(self.cfg)
        self.assertNotIn("humanSummary", result)
        self.assertEqual(self.notifications, [])
        self.assertEqual(idle.human_summary({f"PROJ-{n}": "human-risk" for n in range(10, 20)}).split("\n")[1],
                         "Human check: PROJ-10, PROJ-11, PROJ-12, PROJ-13, PROJ-14, PROJ-15, PROJ-16, PROJ-17 and 2 more")


class ModelRefusalTests(Fixture):
    def setUp(self):
        super().setUp()
        self.data["drain"] = {"enabled": True, "model_refusal": {
            "patterns": ["OUTPUT_SCHEMA_FAILED", "(?i)cybersecurity"], "retry_worker": "reviewer"}}
        self.write("running.json", {"token": "test", "started_at": time.time()})
        self.write("queue.json", {"PROJ-123": {"stage": "build", "waiting": None}})
        self.result_path, self.stderr = self.root / "result.json", self.root / "stderr.txt"
        self.result_path.write_text('{"status":"failed"}')
        self.stderr.write_text("OUTPUT_SCHEMA_FAILED: response blocked by the Cybersecurity policy")

    def check(self, *extra):
        self.save_config()
        response = subprocess.run([sys.executable, "-m", "factory.drain", "--config", str(self.config), "--token", "test",
                                   "check-result", "--key", "PROJ-123", "--result", str(self.result_path),
                                   "--stderr", str(self.stderr), *extra], cwd=ROOT, capture_output=True, text=True, timeout=20)
        return response.returncode, json.loads(response.stdout or "null")

    def test_refusal_names_the_retry_worker(self):
        self.assertEqual(self.check(), (0, {"rateLimited": False, "retryAllowed": True,
                                            "failureType": "model-refusal", "retryWorker": "reviewer"}))

    def test_refusal_keeps_a_failed_gate_retryable_without_a_wait(self):
        (self.root / "verify.json").write_text(json.dumps({"status": "PASS", "env": "dev"}))
        manifest = self.root / "requirements.json"
        manifest.write_text(json.dumps({"key": "PROJ-123", "stage": "built", "checks": [
            {"name": "runtime", "artifact": "verify.json", "resultField": "status", "environment": {"env": "dev"}}]}))
        code, payload = self.check("--requirements", str(manifest))
        self.assertEqual(code, 4)
        self.assertEqual((payload["failureType"], payload["retryWorker"], payload["retryAllowed"]),
                         ("model-refusal", "reviewer", True))
        self.assertNotIn("queueWaiting", payload)
        self.assertTrue(json.loads((self.root / "result.json.gate.json").read_text())["retryAllowed"])

    def test_rate_limit_or_partial_match_is_not_a_refusal(self):
        self.stderr.write_text("OUTPUT_SCHEMA_FAILED cybersecurity HTTP 429")
        self.assertEqual(self.check()[1]["failureType"], "infra")
        self.stderr.write_text("OUTPUT_SCHEMA_FAILED")
        self.assertNotIn("failureType", self.check()[1])
        del self.data["drain"]["model_refusal"]
        self.stderr.write_text("OUTPUT_SCHEMA_FAILED cybersecurity")
        self.assertNotIn("failureType", self.check()[1])

    def test_invalid_refusal_configuration_is_rejected(self):
        for settings in ({"patterns": [], "retry_worker": "reviewer"}, {"patterns": ["("], "retry_worker": "reviewer"},
                         {"patterns": ["x"], "retry_worker": "missing-role"}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                Config({**self.data, "drain": {"enabled": True, "model_refusal": settings}}, self.config)


class WorkerPromptTests(Fixture):
    def test_worker_leaves_cleanup_to_the_orchestrator_and_commits_with_a_body(self):
        brief = self.root / "brief.md"
        brief.write_text("Approved unit")
        result = {"status": "done", "summary": "ok", "key": "PROJ-123", "stage": "built", "waiting": None,
                  "artifacts": [], "checks": []}
        prompts = []

        def launch(command, **kwargs):
            prompts.append(command[-1])
            return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")
        with patch("factory.runtime.subprocess.run", side_effect=launch), redirect_stdout(io.StringIO()):
            self.assertEqual(worker(self.cfg, "builder", "title", brief, key="PROJ-123"), 0)
        prompt = prompts[0]
        self.assertIn("Never remove worktrees or delete merge/* branches yourself", prompt)
        self.assertIn("cleanup:<absolute worktree path>", prompt)
        self.assertIn("factory.cleanup", prompt)
        self.assertIn("-m SUBJECT -m BODY", prompt)
        self.assertNotIn("optional cleanup", prompt)


class DeadOwnerAfterAlertTests(Fixture):
    """A closed owner whose six-hour alert was already sent is reclaimed at once, without a second alert."""

    def test_closed_owner_after_alert_is_reclaimed_silently(self):
        self.data["liveness"] = {"enabled": True}
        self.data["launcher"].update({"list_cmd": ["injected", "list", "{token}"],
                                      "inspect_cmd": ["injected", "inspect", "{id}"]})
        self.cfg = Config(self.data, self.config)
        now = time.time()
        self.write("running.json", {"token": "old", "started_at": now - 30000, "orchestratorId": "owner",
                                    "livenessAlertAt": now - 8000})

        def run(command, **values):
            argv = self.cfg.command(command, **values)
            payload = [] if argv[1] == "list" else {"Status": "closed", "Archived": False}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        with patch.object(self.cfg, "run", side_effect=run), patch.object(idle, "notify") as notify:
            _, run_record = guard(self.cfg, manual=True)
        notify.assert_not_called()
        self.assertIsNotNone(run_record)
        owner = self.read("running.json")
        self.assertEqual(owner["token"], run_record["token"])
        self.assertNotIn("livenessAlertAt", owner)
        actions = [json.loads(line)["action"] for line in (self.state / "runs.jsonl").read_text().splitlines()]
        self.assertEqual(actions, ["lost-run"])
        self.assertTrue((self.state / "runs/old/lost").exists())
