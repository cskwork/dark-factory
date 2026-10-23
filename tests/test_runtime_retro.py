from contextlib import redirect_stdout
from datetime import date
import io
import json
import subprocess
import time
from unittest.mock import patch

from factory.runtime import guard, wake
from support import Fixture


class RuntimeRetroTests(Fixture):
    def setUp(self):
        super().setUp()
        self.data["launcher"].update(list_cmd=["agents", "list"],
                                     archive_cmd=["agents", "archive", "{id}"])
        self.operations = []
        # Cleanup now requires local end-of-run evidence as well as agent status.
        ended = self.state / "runs" / "finished-run" / "ended"
        ended.parent.mkdir(parents=True)
        ended.touch()

    def run_wake(self, agents, *, list_code=0, archive_failure=None, **kwargs):
        if isinstance(agents, (list, dict)):
            rows = agents if isinstance(agents, list) else agents.get("agents", [])
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        row.setdefault("labels", ["run=finished-run"])
        def command(template, **values):
            argv = self.cfg.command(template, **values)
            self.operations.append(argv)
            if argv == ["agents", "list"]:
                if isinstance(agents, Exception):
                    raise agents
                stdout = agents if isinstance(agents, str) else json.dumps(agents)
                return subprocess.CompletedProcess(argv, list_code, stdout, "")
            if archive_failure == argv[-1]:
                raise OSError("archive unavailable")
            return subprocess.CompletedProcess(argv, 0, "", "")

        def launch(argv, **unused):
            self.operations.append(["launch", argv[-1]])
            return subprocess.CompletedProcess(argv, 0, "new-orchestrator", "")

        with patch.object(self.cfg, "run", side_effect=command), \
                patch("factory.runtime.subprocess.run", side_effect=launch), \
                redirect_stdout(io.StringIO()) as output:
            code = wake(self.cfg, **kwargs)
        return code, output.getvalue()

    def test_archives_only_finished_agents_before_launch(self):
        agents = [{"id": status + "-id", "status": status}
                  for status in ("idle", "running", "finished", "completed", "starting")]
        code, output = self.run_wake(agents)
        self.assertEqual(code, 0)
        self.assertEqual(self.operations[:-1], [["agents", "list"],
                                               ["agents", "archive", "idle-id"],
                                               ["agents", "archive", "finished-id"],
                                               ["agents", "archive", "completed-id"]])
        self.assertEqual(self.operations[-1][0], "launch")
        self.assertIn("archived=3 archive_failed=0", output)

    def test_object_agents_list_and_archive_failure_still_launch(self):
        code, output = self.run_wake({"agents": [{"id": "bad", "status": "finished"},
                                                {"id": "good", "status": "idle"}]},
                                     archive_failure="bad")
        self.assertEqual(code, 0)
        self.assertEqual(self.operations[-2], ["agents", "archive", "good"])
        self.assertEqual(self.operations[-1][0], "launch")
        self.assertIn("archived=1 archive_failed=1", output)

    def test_list_failure_and_invalid_output_never_archive_or_block(self):
        for agents, code in (([], 1), ("not-json", 0), ({"agents": {}}, 0),
                             (subprocess.TimeoutExpired("agents", 1), 0)):
            with self.subTest(agents=agents, code=code):
                self.operations.clear()
                result, output = self.run_wake(agents, list_code=code, manual=True)
                self.assertEqual(result, 0)
                self.assertEqual([op[0] for op in self.operations], ["agents", "launch"])
                self.assertIn("archived=0 archive_failed=1", output)
                guard(self.cfg, release=True)

    def test_archive_nonzero_exit_is_counted(self):
        def command(template, **values):
            return subprocess.CompletedProcess([], 1 if values else 0,
                                               json.dumps([{"id": "old", "status": "idle", "labels": ["run=finished-run"]}]), "")

        with patch.object(self.cfg, "run", side_effect=command), \
                patch("factory.runtime.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "new", "")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(wake(self.cfg), 0)
        self.assertIn("archived=0 archive_failed=1", output.getvalue())

    def test_dry_run_never_lists_archives_or_launches(self):
        code, output = self.run_wake([{"id": "old", "status": "idle"}], dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.operations, [])
        self.assertIn("dry-run:", output)

    def test_guard_skip_never_lists_archives_or_launches(self):
        self.write("running.json", {"started_at": time.time(), "token": "other"})
        self.assertEqual(self.run_wake([])[0], 0)
        self.assertEqual(self.operations, [])

    def test_tick_decision_persisted_and_passed_to_prompt(self):
        (self.state / "done-today").write_text(date.today().isoformat())
        result = {"idle": False, "reasons": ["new-events", "queue-runnable:PROJ-123"],
                  "queue": {"PROJ-123": None}, "events": 1}
        with patch("factory.runtime.decide", return_value=(result, 1)):
            self.assertEqual(self.run_wake([])[0], 0)
        decision = self.read("guard-last.json")
        self.assertIsInstance(decision["at"], (int, float))
        self.assertEqual(decision["reasons"], result["reasons"])
        self.assertEqual(decision["queue"], result["queue"])
        prompt = self.operations[-1][-1]
        self.assertIn("guard_reasons=new-events,queue-runnable:PROJ-123", prompt)
        self.assertIn(str(self.state / "guard-last.json"), prompt)

    def test_manual_and_morning_replace_stale_tick_reasons(self):
        for manual, reason in ((True, "manual"), (False, "morning")):
            with self.subTest(manual=manual):
                self.write("guard-last.json", {"at": 1, "reasons": ["stale-tick"], "queue": {}})
                self.operations.clear()
                self.assertEqual(self.run_wake([], manual=manual)[0], 0)
                self.assertEqual(self.read("guard-last.json")["reasons"], [reason])
                self.assertIn("guard_reasons=" + reason, self.operations[-1][-1])
                self.assertNotIn("stale-tick", self.operations[-1][-1])
                guard(self.cfg, release=True)
                (self.state / "last-wake.json").unlink(missing_ok=True)

    def test_idle_skip_persists_latest_decision(self):
        (self.state / "done-today").write_text(date.today().isoformat())
        result = {"idle": True, "reasons": ["no-events", "no-runnable-work"], "events": 0, "queue": {}}
        with patch("factory.runtime.decide", return_value=(result, 0)):
            self.assertIsNone(guard(self.cfg)[1])
        self.assertEqual(self.read("guard-last.json")["reasons"], result["reasons"])

    def test_missing_archive_id_template_does_not_run_cleanup(self):
        self.data["launcher"]["archive_cmd"] = ["agents", "archive-all"]
        code, output = self.run_wake([{"id": "old", "status": "idle"}])
        self.assertEqual(code, 0)
        self.assertEqual([op[0] for op in self.operations], ["launch"])
        self.assertIn("archived=0 archive_failed=1", output)

    def test_failed_tick_launch_retries_same_runnable_content(self):
        (self.state / 'done-today').write_text(date.today().isoformat())
        self.write('queue.json', {'PROJ-123': {'nextStep': 'local', 'waiting': None}})
        self.data['launcher'].pop('list_cmd')
        self.data['launcher'].pop('archive_cmd')
        self.responses['launch']['code'] = 1
        self.save_responses()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(wake(self.cfg), 1)
        self.responses['launch']['code'] = 0
        self.save_responses()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(wake(self.cfg), 0)
        self.assertEqual(sum(call[0] == 'launch' for call in self.calls()), 2)

    def test_tick_guard_preserves_stuck_and_notification_details(self):
        (self.state / 'done-today').write_text(date.today().isoformat())
        result = {'idle': True, 'reasons': ['no-runnable-work'], 'events': 0,
                  'queue': {'PROJ-123': None}, 'stuck': ['stuck-runnable:PROJ-123'],
                  'waitingHuman': [{'action': 'Connect network', 'keys': ['PROJ-2']}],
                  'notify': 'failed'}
        with patch('factory.runtime.decide', return_value=(result, 0)):
            self.assertIsNone(guard(self.cfg)[1])
        persisted = self.read('guard-last.json')
        for name in ('stuck', 'waitingHuman', 'notify'):
            self.assertEqual(persisted[name], result[name])
