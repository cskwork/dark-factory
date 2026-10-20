from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

from factory.config import Config
from factory import decision_page as dp


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config({"project_root": str(self.root), "state_dir": "state",
                           "decisions": {"approval": {"on_approve": ["example", "{key}", "{decision_id}", "{note}"]}}},
                          self.root / "factory.toml")
        self.woken, self.actions = [], []
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def spec(self, kind="approval"):
        spec = {"kind": kind, "key": "TASK-1", "title": "Review <change>", "summary": "Prepared change"}
        if kind == "choice":
            spec.update(question="Which action?", options=[{"id": "wait", "label": "Wait"},
                                                          {"id": "skip", "label": "Skip"}])
        if kind == "sql-run":
            source = self.root / "change.sql"
            source.write_text("UPDATE items SET n=1;", encoding="utf-8")
            spec.update(file=str(source), config="test", expect_host="test-", guard_sql="SELECT 0", guard_expect=0)
        return dp.create_request(self.cfg, spec)

    def action(self, spec, note):
        self.actions.append((spec["kind"], note))
        return {"ok": True, "detail": "complete"}

    def start(self, spec, action=None):
        server, token, _ = dp.make_server(self.cfg, spec, actions={"approval": action or self.action,
                                                                 "sql-run": action or self.action}, wake=self.woken.append)
        self.servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}", token

    def post(self, base, token, option="approve", **headers):
        req = urllib.request.Request(base + "/decide", data=urllib.parse.urlencode({"t": token, "option": option}).encode(),
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read().decode()

    def events(self):
        return [json.loads(line) for line in (self.cfg.state_dir / "decisions.jsonl").read_text().splitlines()]

    def test_get_token_html_escape_and_no_consumption(self):
        spec = self.spec()
        base, token = self.start(spec)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/?t=wrong", timeout=5)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        with urllib.request.urlopen(base + "/?t=" + token, timeout=5) as response:
            self.assertIn("Review &lt;change&gt;", response.read().decode())
        self.assertFalse(self.actions)
        self.assertFalse(list((self.cfg.state_dir / "decisions").glob("*.claimed")))
        self.assertEqual(self.post(base, token)[0], 200)

    def test_replay_across_two_servers_executes_once(self):
        spec = self.spec()
        first, token = self.start(spec)
        second, _ = self.start(spec)
        self.post(first, token)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post(second, token)
        self.assertEqual(error.exception.code, 409)
        error.exception.close()
        self.assertEqual(len(self.actions), 1)
        self.assertEqual(self.woken, ["TASK-1"])

    def test_concurrent_claim_executes_once(self):
        spec = self.spec("sql-run")
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: dp.decide(self.cfg, spec, "approve", actions={"sql-run": self.action},
                                                            wake=self.woken.append), range(8)))
        self.assertEqual(sum(r["status"] == "done" for r in results), 1)
        self.assertEqual(len(self.actions), 1)

    def test_invalid_choice_does_not_consume_token(self):
        spec = self.spec("choice")
        with self.assertRaises(ValueError):
            dp.decide(self.cfg, spec, "invalid")
        result = dp.decide(self.cfg, spec, "skip", wake=self.woken.append)
        self.assertTrue(result["ok"])
        self.assertEqual(self.events()[0]["option"], "skip")

    def test_action_exception_audited_and_wakes(self):
        def failure(*args):
            raise RuntimeError("command failed")
        result = dp.decide(self.cfg, self.spec(), "approve", actions={"approval": failure}, wake=self.woken.append)
        self.assertFalse(result["ok"])
        self.assertEqual(self.woken, ["TASK-1"])
        self.assertEqual([e["event"] for e in self.events()], ["decision", "action", "wake"])
        self.assertIn("RuntimeError", self.events()[1]["error"])

    def test_cross_origin_and_host_header_refused_without_consumption(self):
        base, token = self.start(self.spec())
        for headers in ({"Origin": "https://elsewhere.example"}, {"Host": "elsewhere.example"}):
            with self.subTest(headers=headers), self.assertRaises(urllib.error.HTTPError) as error:
                self.post(base, token, **headers)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        self.assertFalse(self.actions)
        self.post(base, token)
        self.assertEqual(len(self.actions), 1)

    def test_wake_failure_retained_and_retried_without_action(self):
        def fail(key):
            raise OSError("wake unavailable")
        spec = self.spec()
        result = dp.decide(self.cfg, spec, "approve", actions={"approval": self.action}, wake=fail)
        self.assertFalse(result["wake_ok"])
        pending = list((self.cfg.state_dir / "decisions").glob("*.wake-pending.json"))
        self.assertEqual(len(pending), 1)
        self.assertTrue(json.loads(pending[0].read_text())["wake_pending"])
        self.assertEqual(dp.retry_pending_wakes(self.cfg, wake=self.woken.append), [True])
        self.assertTrue(json.loads(pending[0].read_text())["wake_delivered_at"])
        self.assertEqual(dp.retry_pending_wakes(self.cfg, wake=self.woken.append), [])
        self.assertEqual(len(self.actions), 1)

    def test_duplicate_wake_exit_zero_stays_pending(self):
        result = dp.decide(self.cfg, self.spec(), "approve", actions={"approval": self.action},
                           wake=lambda key: subprocess.CompletedProcess([], 0, "skipping duplicate", ""))
        self.assertFalse(result["wake_ok"])
        self.assertEqual(len(list((self.cfg.state_dir / "decisions").glob("*.wake-pending.json"))), 1)

    def test_approval_shape_command_and_unused_conflict(self):
        spec = self.spec()
        with patch.object(self.cfg, "run", return_value=subprocess.CompletedProcess([], 0, "done", "")) as command:
            result = dp.decide(self.cfg, spec, "approve", wake=self.woken.append)
        self.assertTrue(result["ok"])
        approval = json.loads((self.cfg.state_dir / "approvals.json").read_text())["TASK-1"]
        self.assertEqual(approval["decision_id"], spec["decision_id"])
        self.assertTrue(approval["approved"])
        self.assertIsNone(approval["used_at"])
        self.assertEqual(command.call_args.kwargs["key"], "TASK-1")
        result = dp.decide(self.cfg, self.spec(), "approve", wake=self.woken.append)
        self.assertFalse(result["ok"])
        self.assertIn("unused approval", result["error"])

    def test_sql_file_hash_binding_refuses_change(self):
        spec = self.spec("sql-run")
        self.assertEqual(spec["sql_sha256"], hashlib.sha256(b"UPDATE items SET n=1;").hexdigest())
        Path(spec["file"]).write_text("DELETE FROM items;")
        with patch.object(dp.sql_runner, "run") as runner:
            result = dp.decide(self.cfg, spec, "approve", wake=self.woken.append)
        self.assertFalse(result["ok"])
        runner.assert_not_called()
        self.assertEqual(self.woken, ["TASK-1"])

    def test_sql_snapshot_and_configuration_bound(self):
        spec = self.spec("sql-run")
        with patch.object(dp.sql_runner, "run", return_value={"status": "done"}) as runner:
            result = dp.decide(self.cfg, spec, "approve", wake=self.woken.append)
        self.assertTrue(result["ok"])
        self.assertEqual(runner.call_args.kwargs["config_name"], "test")
        self.assertEqual(runner.call_args.kwargs["sql_text"], "UPDATE items SET n=1;")

    def test_failed_approval_command_invalidates_permission_and_redacts_stderr(self):
        spec = self.spec()
        with patch.object(self.cfg, "run", return_value=subprocess.CompletedProcess([], 7, "", "password=secret")):
            result = dp.decide(self.cfg, spec, "approve", wake=self.woken.append)
        self.assertFalse(result["ok"])
        self.assertIn("exited 7", result["error"])
        self.assertNotIn("secret", json.dumps(self.events()))
        approval = json.loads((self.cfg.state_dir / "approvals.json").read_text())["TASK-1"]
        self.assertFalse(approval["approved"])

    def test_approval_remains_unusable_until_command_succeeds(self):
        entered, release = threading.Event(), threading.Event()

        def blocked_command(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release timed out")
            return subprocess.CompletedProcess([], 0, "", "")

        spec = self.spec()
        with patch.object(self.cfg, "run", side_effect=blocked_command), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(dp.decide, self.cfg, spec, "approve", wake=self.woken.append)
            try:
                self.assertTrue(entered.wait(5))
                approval = json.loads((self.cfg.state_dir / "approvals.json").read_text())["TASK-1"]
                self.assertFalse(approval["approved"])
                self.assertEqual(approval["action_status"], "pending")
                duplicate = dp.decide(self.cfg, self.spec(), "approve", wake=self.woken.append)
                self.assertFalse(duplicate["ok"])
                self.assertIn("already pending", duplicate["error"])
            finally:
                release.set()
            self.assertTrue(future.result(timeout=5)["ok"])
        approval = json.loads((self.cfg.state_dir / "approvals.json").read_text())["TASK-1"]
        self.assertTrue(approval["approved"])
        self.assertEqual(approval["action_status"], "done")

    def test_invalid_key_rejected_before_request_persistence(self):
        with self.assertRaises(ValueError):
            dp.create_request(self.cfg, {"kind": "approval", "key": "../../bad"})

    def test_detached_real_cli_returns_url_and_server_stops_after_decision(self):
        self.cfg.path.write_text(f"project_root = {json.dumps(str(self.root))}\nstate_dir = 'state'\n")
        start = time.monotonic()
        result = subprocess.run([sys.executable, "-m", "factory.decision_page", "--config", str(self.cfg.path),
                                 "choice", "TASK-1", "--title", "Choose", "--options-json",
                                 '[{"id":"wait","label":"Wait"}]', "--timeout", "5"],
                                cwd=Path(dp.__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - start, 4)
        url = result.stdout.strip()
        parsed = urllib.parse.urlparse(url)
        token = urllib.parse.parse_qs(parsed.query)["t"][0]
        self.assertEqual(self.post(f"{parsed.scheme}://{parsed.netloc}", token, "wait")[0], 200)
        deadline = time.monotonic() + 5
        stopped = False
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=.3):
                    pass
            except (OSError, urllib.error.URLError):
                stopped = True
                break
            time.sleep(.05)
        self.assertTrue(stopped, "detached server did not stop after decision")
        for path in (self.cfg.state_dir, self.cfg.state_dir / "decisions", self.cfg.state_dir / "decision-pages"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        for path in (self.cfg.state_dir / "decisions").glob("*.claimed"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_internal_serve_cli_has_bounded_timeout(self):
        self.cfg.path.write_text(f"project_root = {json.dumps(str(self.root))}\nstate_dir = 'state'\n")
        result = subprocess.run([sys.executable, "-m", "factory.decision_page", "--config", str(self.cfg.path),
                                 "choice", "TASK-1", "--serve", "--options-json",
                                 '[{"id":"wait","label":"Wait"}]', "--timeout", ".1"],
                                cwd=Path(dp.__file__).resolve().parents[1], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.startswith("http://127.0.0.1:"))


if __name__ == "__main__":
    unittest.main()
