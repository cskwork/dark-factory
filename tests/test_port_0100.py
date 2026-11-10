"""0.10.0: lost-run diagnostics, tracked fix-now lessons and human-only action waits. Offline only."""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
import subprocess
import time
from unittest.mock import Mock, patch

from factory import decision_inbox, decision_page, idle, judge, lessons, state
from factory.config import Config
from factory.runtime import guard
from support import Fixture


class FakeTodo:
    """An in-memory sdlc-kit to-do list recording every command."""

    def __init__(self):
        self.items, self.commands = {}, []

    def __call__(self, args, timeout=60):
        self.commands.append(list(args))
        verb = args[0]
        if verb == "add":
            identity = f"t{len(self.items) + 1}"
            self.items[identity] = {"id": identity, "title": args[1], "status": "open", "notes": []}
            return identity
        if verb == "list":
            return [dict(item) for item in self.items.values()]
        if verb in ("note", "wait"):
            self.items[args[1]]["notes"].append(args[2])
        if verb == "done":
            self.items[args[1]]["status"] = "done"
        return ""

    def verbs(self, verb):
        return [command for command in self.commands if command[0] == verb]


class LostRunDiagnostics(Fixture):
    def setUp(self):
        super().setUp()
        self.data["liveness"] = {"enabled": True}
        self.data["launcher"].update({"list_cmd": ["injected", "list", "{token}"],
                                      "inspect_cmd": ["injected", "inspect", "{id}"]})
        adapter = patch.object(self.cfg, "run", side_effect=lambda command, **values: subprocess.CompletedProcess(
            command, 0, "[]", ""))
        adapter.start()
        self.addCleanup(adapter.stop)

    def test_running_json_records_trigger(self):
        self.assertEqual(guard(self.cfg, manual=True)[1]["trigger"], "manual")
        self.assertEqual(self.read("running.json")["trigger"], "manual")

    def test_lost_run_lesson_records_orchestrator_id_and_trigger(self):
        # A wake that died after taking the lock leaves no orchestratorId; the lesson must say so.
        self.write("running.json", {"token": "old", "started_at": time.time() - 900, "trigger": "tick"})
        self.assertIsNotNone(guard(self.cfg, manual=True)[1])
        lesson = json.loads((self.state / "lessons.jsonl").read_text().splitlines()[0])
        self.assertEqual(lesson["what"], "lost-run: old")
        self.assertEqual((lesson["orchestratorId"], lesson["trigger"]), (None, "tick"))


class FixNowTracking(Fixture):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 11, 6, tzinfo=timezone.utc)
        self.todo = FakeTodo()
        self.opened = []
        self.opener = Mock(side_effect=self.open_decision)

    def open_decision(self, cfg, path):
        with patch.object(decision_page, "_stamp", return_value=self.now.isoformat()):
            spec = decision_page.create_request(cfg, json.loads(path.read_text()))
        self.opened.append(spec)
        return spec

    def add(self, **changes):
        row = {"kind": "infra_fail", "what": "PROJ-123 gate failed", "at": self.now.isoformat(),
               "evidence": "runs/gate.json", **changes}
        return lessons.append_lesson(self.cfg, row, now=self.now, opener=self.opener, todo=self.todo)

    def answer(self, spec, option):
        with patch.object(decision_page, "_stamp", return_value=self.now.isoformat()), \
                patch.object(lessons, "_todo", return_value=self.todo):
            return decision_page.decide(self.cfg, spec, option, wake=lambda key: None)

    def fix_now(self):
        for _ in range(3):
            self.add()
        self.answer(self.opened[0], "fix-now")
        return lessons.signature({"kind": "infra_fail", "what": "PROJ-123 gate failed"})

    def test_fix_now_answer_opens_one_tracked_item(self):
        sig = self.fix_now()
        self.assertEqual(len(self.todo.verbs("add")), 1)
        self.assertTrue(self.todo.verbs("add")[0][1].startswith(lessons.FIX_TITLE + "PROJ-123 gate failed"))
        self.assertIn(f"lesson-fixed {sig[:12]} --commit", self.todo.verbs("wait")[0][2])
        fix = self.read("lesson-decisions/fixes.json")[sig]
        self.assertEqual((fix["state"], fix["todoId"], fix["decision"]), ("open", "t1", self.opened[0]["decision_id"]))
        self.assertEqual(lessons.track_fixes(self.cfg, todo=self.todo), {sig: "t1"})
        self.assertEqual(len(self.todo.verbs("add")), 1)

    def test_recurrence_while_open_adds_one_note_per_day_and_no_decision(self):
        self.fix_now()
        notes = len(self.todo.verbs("note"))
        self.assertEqual(self.add()["decision"], "fix-tracked")
        self.assertEqual(self.add()["decision"], "fix-tracked")
        self.assertEqual(len(self.todo.verbs("note")), notes + 1)
        self.now += timedelta(days=1)
        self.add()
        self.assertEqual(len(self.todo.verbs("note")), notes + 2)
        self.assertEqual(self.opener.call_count, 1)

    def test_lesson_fixed_closes_item_and_recurrence_opens_fix_failed_decision(self):
        sig = self.fix_now()
        with self.assertRaisesRegex(ValueError, "commit"):
            lessons.mark_fixed(self.cfg, sig[:12], "not-a-sha", todo=self.todo)
        result = lessons.mark_fixed(self.cfg, sig[:12], "abc1234", now=self.now, todo=self.todo)
        self.assertEqual(result, {"signature": sig, "state": "fixed", "commit": "abc1234"})
        self.assertEqual(self.todo.items["t1"]["status"], "done")
        self.now += timedelta(hours=1)
        # One occurrence, inside the seven-day cooldown of the fix-now answer: still asked at once.
        self.assertEqual(self.add()["decision"], "opened-fix-failed")
        self.assertIn("did not hold", self.opened[-1]["summary"])
        self.assertIn("commit abc1234", self.opened[-1]["summary"])
        self.assertEqual(self.add()["decision"], "already-open")
        self.assertEqual(self.opener.call_count, 2)

    def test_person_closing_the_item_closes_the_fix(self):
        sig = self.fix_now()
        self.todo.items["t1"]["status"] = "done"
        self.now += timedelta(hours=1)
        self.assertEqual(self.add()["decision"], "opened-fix-failed")
        self.assertEqual(self.read("lesson-decisions/fixes.json")[sig]["state"], "closed")

    def test_state_cli_lesson_fixed(self):
        sig = self.fix_now()
        self.save_config()
        stdout = io.StringIO()
        with patch.object(lessons, "_todo", return_value=self.todo), contextlib.redirect_stdout(stdout):
            self.assertEqual(state.main(["--config", str(self.config), "lesson-fixed", sig[:12],
                                         "--commit", "abc1234"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["state"], "fixed")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(state.main(["--config", str(self.config), "lesson-fixed", sig[:12],
                                         "--commit", "abc1234"]), 1)

    def test_todo_failure_keeps_the_lesson_and_is_recorded(self):
        self.fix_now()
        broken = Mock(side_effect=RuntimeError("to-do unavailable"))
        result = lessons.append_lesson(self.cfg, {"kind": "infra_fail", "what": "PROJ-123 gate failed",
                                                  "at": self.now.isoformat()}, now=self.now, todo=broken)
        self.assertEqual(result["decision"], "fix-tracked")
        self.assertEqual(result["fixTrackError"], "RuntimeError: to-do unavailable")
        self.assertIn("to-do unavailable", self.read("lesson-decisions/fixes.error.json")["error"])
        self.assertEqual(len((self.state / "lessons.jsonl").read_text().splitlines()), 4)
        self.add()
        self.assertFalse((self.state / "lesson-decisions/fixes.error.json").exists())

    def test_html_surface_tracks_without_an_item(self):
        for _ in range(3):
            lessons.append_lesson(self.cfg, {"kind": "infra_fail", "what": "PROJ-123 gate failed",
                                             "at": self.now.isoformat()}, now=self.now, opener=self.opener)
        with patch.object(decision_page, "_stamp", return_value=self.now.isoformat()):
            decision_page.decide(self.cfg, self.opened[0], "fix-now", wake=lambda key: None)
        fixes = self.read("lesson-decisions/fixes.json")
        self.assertEqual([(fix["state"], fix["todoId"]) for fix in fixes.values()], [("open", None)])


class HumanAction(Fixture):
    def patch_queue(self, *args):
        self.save_config()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return state.main(["--config", str(self.config), "patch", "queue.json", "--path", '["PROJ-1"]', *args])

    def test_action_replaces_question_and_suggest(self):
        self.write("queue.json", {"PROJ-1": {"waiting": "human-risk", "question": "Create it?", "suggest": "Yes"}})
        self.assertEqual(self.patch_queue("--json", '{"waiting":"human-risk"}', "--action", "Create the API secret",
                                          "--run-sheet", "docs/secret.md"), 0)
        item = self.read("queue.json")["PROJ-1"]
        self.assertEqual((item["action"], item["runSheet"]), ("Create the API secret", "docs/secret.md"))
        self.assertNotIn("question", item)
        self.assertNotIn("suggest", item)
        self.assertEqual(self.patch_queue("--json", '{"question":"Retry?","suggest":"Retry"}'), 0)
        self.assertNotIn("action", self.read("queue.json")["PROJ-1"])

    def test_action_excludes_suggest_and_run_sheet_needs_action(self):
        self.assertEqual(self.patch_queue("--json", '{"suggest":"Yes"}', "--action", "Create the secret"), 1)
        self.assertEqual(self.patch_queue("--json", "{}", "--run-sheet", "docs/secret.md"), 1)
        self.assertFalse((self.state / "queue.json").exists())

    def test_judge_skips_action_waits(self):
        cfg = Config({**self.data, "judge": {"mode": "on", "command": ["model", "{schema}"]}}, self.config)
        queue = {"PROJ-1": {"waiting": "human-risk", "question": "Done?", "suggest": "Proceed",
                            "action": "Create the API secret"},
                 "PROJ-2": {"waiting": "human-risk", "question": "Retry?", "suggest": "Retry"}}
        with patch.dict(os.environ, {"FACTORY_JUDGE_MODE": "on"}):
            self.assertEqual(judge.risk_candidates(cfg, queue, set()), ["PROJ-2"])

    def test_guard_shows_the_action_without_a_suggested_answer(self):
        self.write("queue.json", {"PROJ-1": {"waiting": "human-risk", "action": "Create the API secret",
                                             "runSheet": "docs/secret.md"}})
        result, code = idle.decide(self.cfg)
        self.assertEqual(code, 0)
        self.assertEqual(result["waitingHuman"], [{"action": "Human action: Create the API secret · run sheet: "
                                                             "docs/secret.md", "keys": ["PROJ-1"]}])
