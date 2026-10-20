from contextlib import redirect_stdout
from datetime import date
import io
import json
import subprocess
import time
from unittest.mock import patch
from factory.runtime import guard, wake, worker
from support import Fixture, event


class GuardTests(Fixture):
    def test_bookkeeping_failure_does_not_leave_unlaunched_lock(self):
        from factory.state import write_json
        def write(path, value):
            if path.name == "last-wake.json":
                raise OSError("disk full")
            return write_json(path, value)
        with patch("factory.runtime.write_json", side_effect=write):
            with self.assertRaises(OSError):
                guard(self.cfg)
        self.assertFalse((self.state / "running.json").exists())
        self.assertIsNotNone(guard(self.cfg, manual=True)[1])

    def tick(self):
        (self.state / "done-today").write_text(date.today().isoformat())

    def test_morning_does_not_scan(self):
        message, run = guard(self.cfg)
        self.assertEqual(message, "mode=morning")
        self.assertTrue(run["token"])
        self.assertEqual(self.calls(), [])

    def test_idle_before_running_lock(self):
        self.tick()
        message, run = guard(self.cfg)
        self.assertIn("skipping duplicate (idle", message)
        self.assertIsNone(run)
        self.assertFalse((self.state / "running.json").exists())
        self.assertIn("idle-skip(guard)", (self.state / "runs.jsonl").read_text())
        self.assertIn("skipping duplicate (window)", guard(self.cfg)[0])

    def test_manual_bypasses_idle_and_window_but_respects_lock(self):
        self.tick()
        self.write("last-wake.json", {"at": time.time()})
        message, run = guard(self.cfg, manual=True)
        self.assertIn("manual run", message)
        self.assertEqual(self.calls(), [])
        self.assertIn("skipping duplicate (running)", guard(self.cfg, manual=True)[0])
        self.assertEqual(self.read("running.json"), run)

    def test_duplicate_window_precedes_scan(self):
        self.tick()
        self.write("last-wake.json", {"at": time.time()})
        self.assertIn("skipping duplicate (window)", guard(self.cfg)[0])
        self.assertEqual(self.calls(), [])

    def test_stale_lock_recovered(self):
        self.write("running.json", {"started_at": time.time() - 11000, "token": "old"})
        self.assertIsNotNone(guard(self.cfg)[1])

    def test_release_and_old_owner_cannot_release_new(self):
        _, run = guard(self.cfg)
        self.assertIn("mismatch", guard(self.cfg, release=True, token="old")[0])
        self.assertTrue((self.state / "running.json").exists())
        self.assertIn("released", guard(self.cfg, release=True, token=run["token"])[0])
        self.assertFalse((self.state / "running.json").exists())

    def test_scan_error_continues_to_lock(self):
        self.tick()
        self.scan(stdout="broken")
        self.assertIsNotNone(guard(self.cfg)[1])

    def test_concurrent_manual_processes_only_one_acquires(self):
        self.save_config()
        from support import ROOT
        commands = [subprocess.Popen([str(ROOT / "bin/factory-guard"), "--config", str(self.config), "--manual"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
        outputs = [p.communicate(timeout=15)[0] for p in commands]
        self.assertEqual(sum("mode=" in text for text in outputs), 1)


class WakeTests(Fixture):
    def test_logging_failure_after_launch_keeps_lock(self):
        with patch("factory.runtime.append_json", side_effect=OSError("disk full")), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(wake(self.cfg), 1)
        self.assertIn("lock retained", output.getvalue())
        self.assertTrue((self.state / "running.json").exists())
        self.assertIn("skipping duplicate (running)", guard(self.cfg, manual=True)[0])

    def test_detached_launch_includes_role_and_lock_context(self):
        self.assertEqual(self.cli("factory-wake").returncode, 0)
        argv = self.calls()[-1]
        self.assertIn("--no-wait", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "model-large")
        self.assertIn("lock_token=", argv[-1])
        self.assertTrue((self.state / "running.json").exists())

    def test_manual_focus(self):
        response = self.cli("factory-wake", "--now", "--key", "PROJ-123")
        self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
        self.assertIn("focus=PROJ-123", self.calls()[-1][-1])
        self.assertIn("trigger=manual", self.calls()[-1][-1])

    def test_invalid_manual_key_never_launches(self):
        self.assertEqual(self.cli("factory-wake", "--key", "PROJ-123").returncode, 2)
        self.assertEqual(self.cli("factory-wake", "--now", "--key", "../bad").returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_skip_never_launches(self):
        self.write("running.json", {"started_at": time.time(), "token": "other"})
        self.assertIn("skipping duplicate", self.cli("factory-wake").stdout)
        self.assertEqual(self.calls(), [])

    def test_launch_failure_and_empty_identifier_release_and_allow_retry(self):
        for response in ({"code": 1}, {"stdout": ""}):
            self.responses["launch"] = response
            self.save_responses()
            result = self.cli("factory-wake")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertFalse((self.state / "running.json").exists())
            self.assertFalse((self.state / "last-wake.json").exists())

    def test_dry_run_releases(self):
        result = self.cli("factory-wake", "--dry-run")
        self.assertEqual(result.returncode, 0)
        self.assertIn("dry-run:", result.stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.state / "running.json").exists())


class WorkerTests(Fixture):
    def setUp(self):
        super().setUp()
        self.brief = self.root / "brief with spaces.md"
        self.brief.write_text("Implement the approved unit.")
        self.result = {"status": "done", "summary": "Verified", "key": "PROJ-123", "stage": "built", "waiting": None,
                       "artifacts": ["check.json"], "checks": [{"name": "unit", "result": "PASS"}]}
        self.responses["launch"] = {"stdout": json.dumps(self.result)}
        self.save_responses()

    def test_mapping_schema_and_brief(self):
        for role, model in (("builder", "model-small"), ("reviewer", "model-review")):
            response = self.cli("factory-worker", role, "Title with spaces", str(self.brief))
            self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
            self.assertEqual(json.loads(response.stdout), self.result)
            argv = self.calls()[-1]
            self.assertEqual(argv[argv.index("--model") + 1], model)
            self.assertIn("--output-schema", argv)
            self.assertNotIn("--no-wait", argv)
            self.assertIn(self.brief.read_text(), argv[-1])

    def test_unknown_role_or_missing_brief(self):
        self.assertEqual(self.cli("factory-worker", "unknown", "title", str(self.brief)).returncode, 2)
        self.assertEqual(self.cli("factory-worker", "waker", "title", str(self.brief)).returncode, 2)
        self.assertEqual(self.cli("factory-worker", "builder", "title", "/missing/brief").returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_malformed_output_not_accepted(self):
        self.responses["launch"] = {"stdout": "{}"}
        self.save_responses()
        self.assertEqual(self.cli("factory-worker", "builder", "title", str(self.brief)).returncode, 2)

    def test_worker_failure_propagates(self):
        self.responses["launch"] = {"code": 3}
        self.save_responses()
        self.assertEqual(self.cli("factory-worker", "builder", "title", str(self.brief)).returncode, 3)
