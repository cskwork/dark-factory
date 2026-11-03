"""To-do decision surface: replies on sdlc-kit to-do items decide; html never touches the list."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from support import ROOT
from factory.config import Config
from factory import decision_inbox as inbox
from factory import decision_page as dp
from factory import idle

KIT = Path(os.environ.get("SDLC_KIT") or Path.home() / ".agents/skills/sdlc-kit").expanduser()
HAS_KIT = bool(shutil.which("node")) and (KIT / "todo/todo.mjs").exists()


class FakeTodo:
    """In-memory stand-in for the to-do CLI: add/note/wait/done/list --json with human replies."""

    def __init__(self):
        self.items, self.calls = {}, []

    def __call__(self, args):
        self.calls.append(args)
        command = args[0]
        if command == "add":
            item = f"i{len(self.items) + 1}"
            self.items[item] = {"id": item, "title": args[1], "status": "workable", "waiting": "",
                                "replies": [], "notes": []}
            return item
        if command == "list":
            return [dict(row) for row in self.items.values()]
        row = self.items[args[1]]
        if command == "note":
            row["notes"].append(args[2])
        elif command == "wait":
            row.update(status="waiting", waiting=args[2], replies=[])
        elif command == "done":
            row.update(status="done", result=args[2] if len(args) > 2 else "")
        return ""

    def reply(self, item, words, at="2026-11-03 10:00"):
        self.items[item]["replies"].append({"at": at, "who": "human", "what": "reply — " + words})
        self.items[item].update(status="workable", waiting="")


class Base(unittest.TestCase):
    surface = "app"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cfg = Config({"project_root": str(self.root), "state_dir": "state", "key_pattern": r"[A-Z]+-\d+",
                           "decisions": {"surface": self.surface,
                                         "approval": {"on_approve": ["example", "{key}"]}}},
                          self.root / "factory.toml")
        self.woken, self.ran = [], []

    def spec(self, kind="choice"):
        spec = {"kind": kind, "key": "TASK-1", "title": "Pick a path", "summary": "Prepared change"}
        if kind == "choice":
            spec.update(question="Which action?", recommended="skip",
                        options=[{"id": "wait", "label": "Wait"},
                                 {"id": "skip", "label": "Skip", "detail": "Leave it out of this release"}])
        if kind == "sql-run":
            source = self.root / "change.sql"
            source.write_text("UPDATE items SET n=1;", encoding="utf-8")
            spec.update(file=str(source), config="sandbox", expect_host="sandbox-", guard_sql="SELECT 0", guard_expect=0)
        return dp.create_request(self.cfg, spec)

    def action(self, spec, note):
        self.ran.append((spec["kind"], note))
        return {"ok": True, "detail": "complete"}

    def poll(self, todo):
        return inbox.poll(self.cfg, todo=todo, wake=self.woken.append,
                          actions={"approval": self.action, "sql-run": self.action})

    def events(self):
        path = self.cfg.state_dir / "decisions.jsonl"
        return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []


class Parsing(Base):
    def test_surface_default_values_and_invalid(self):
        self.assertEqual(inbox.surface(Config({}, self.root / "factory.toml")), "html")
        for value in inbox.SURFACES:
            with self.subTest(value=value):
                self.assertEqual(inbox.surface(Config({"decisions": {"surface": value}}, self.root / "f.toml")), value)
        for value in ("chrome", "", "APP", 1):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "decisions.surface"):
                Config({"decisions": {"surface": value}}, self.root / "f.toml")

    def test_option_forms(self):
        spec = self.spec()
        for words, expected in (("2", "skip"), ("2번", "skip"), ("D12 1", "wait"), ("d3: 2", "skip"),
                                ("Skip", "skip"), ("wait", "wait"), ("hold", None), ("보류", None)):
            with self.subTest(words=words):
                self.assertEqual(inbox.option_for(spec, words), expected)
        for words in ("3", "0", "maybe"):
            with self.subTest(words=words), self.assertRaises(ValueError):
                inbox.option_for(spec, words)
        approval = self.spec("approval")
        for words in ("approve", "Approve", "1", "승인", "D4 approve"):
            with self.subTest(words=words):
                self.assertEqual(inbox.option_for(approval, words), "approve")
        with self.assertRaises(ValueError):
            inbox.option_for(approval, "2")


class FakeSurface(Base):
    def test_choice_reply_records_once_and_closes_item(self):
        todo = FakeTodo()
        spec = self.spec()
        code, item = inbox.open_item(self.cfg, spec, todo=todo)
        row = todo.items[item]
        self.assertEqual((code, row["status"]), ("D1", "waiting"))
        self.assertTrue(row["title"].startswith("[decision D1] TASK-1"))
        self.assertIn("2) Skip (recommended)", row["waiting"])
        self.assertIn("2) Skip: Leave it out of this release", row["notes"])
        self.assertEqual(self.poll(todo), [])

        todo.reply(item, "2")
        self.assertEqual(self.poll(todo), [(code, "decided")])
        self.assertEqual(todo.items[item]["status"], "done")
        decisions = [e for e in self.events() if e["event"] == "decision"]
        self.assertEqual([(e["option"], e["by"]) for e in decisions], [("skip", "todo")])
        self.assertTrue(decisions[0]["messageId"].startswith(f"todo:{item}:"))
        self.assertEqual(self.woken, ["TASK-1"])
        # The same reply seen again (e.g. the item reopened) is not recorded twice.
        todo.items[item]["status"] = "workable"
        self.assertEqual(self.poll(todo), [])
        result = dp.decide(self.cfg, spec, "skip", wake=self.woken.append, by="todo")
        self.assertEqual(result["status"], "used")
        self.assertEqual(len([e for e in self.events() if e["event"] == "decision"]), 1)

    def test_crash_after_recording_resumes_without_second_record(self):
        todo = FakeTodo()
        code, item = inbox.open_item(self.cfg, self.spec(), todo=todo)
        todo.reply(item, "1")
        failing = FakeTodo.__call__

        def crash_on_done(args):
            if args[0] == "done":
                raise RuntimeError("to-do unavailable")
            return failing(todo, args)
        with self.assertRaises(RuntimeError):
            self.poll(crash_on_done)
        self.assertEqual(self.poll(todo), [(code, "closed")])
        self.assertEqual(todo.items[item]["status"], "done")
        self.assertEqual(len([e for e in self.events() if e["event"] == "decision"]), 1)

    def test_invalid_reply_asks_again_then_hold_records_nothing(self):
        todo = FakeTodo()
        code, item = inbox.open_item(self.cfg, self.spec(), todo=todo)
        todo.reply(item, "maybe")
        self.assertEqual(self.poll(todo), [(code, "invalid")])
        self.assertEqual(todo.items[item]["status"], "waiting")
        self.assertIn("Could not use 'maybe'", todo.items[item]["waiting"])
        self.assertIn("number from 1 to 2", todo.items[item]["waiting"])
        todo.reply(item, "hold")
        self.assertEqual(self.poll(todo), [(code, "held")])
        self.assertEqual(todo.items[item]["status"], "done")
        self.assertEqual(self.events(), [])
        self.assertFalse(list((self.cfg.state_dir / "decisions").glob("*.claimed")))
        self.assertEqual(self.woken, [])

    def test_execution_approvals_run_injected_action_from_reply(self):
        for kind, words in (("approval", "승인"), ("sql-run", "approve")):
            with self.subTest(kind=kind):
                todo = FakeTodo()
                self.ran.clear()
                code, item = inbox.open_item(self.cfg, self.spec(kind), todo=todo)
                self.assertIn("reply 'approve' or 'hold'", todo.items[item]["waiting"])
                todo.reply(item, words)
                self.assertEqual(self.poll(todo), [(code, "decided")])
                self.assertEqual([k for k, _ in self.ran], [kind])
                self.assertIn(f"Human replied '{words}'", self.ran[0][1])
                self.assertIn("Approved and ran", todo.items[item]["result"])

    def test_failed_action_is_reported_on_the_item(self):
        todo = FakeTodo()
        code, item = inbox.open_item(self.cfg, self.spec("approval"), todo=todo)
        todo.reply(item, "approve")
        result = inbox.poll(self.cfg, todo=todo, wake=self.woken.append,
                            actions={"approval": lambda spec, note: {"ok": False, "detail": "refused"}})
        self.assertEqual(result, [(code, "failed")])
        self.assertIn("Action failed", todo.items[item]["result"])

    def test_item_closed_without_reply_records_nothing(self):
        todo = FakeTodo()
        code, item = inbox.open_item(self.cfg, self.spec(), todo=todo)
        todo.items[item]["status"] = "done"
        self.assertEqual(self.poll(todo), [(code, "closed")])
        self.assertFalse(inbox.open_items(self.cfg))
        self.assertEqual(self.events(), [])

    def test_alert_reuses_one_item_and_notify_keeps_its_status(self):
        todo = FakeTodo()
        self.assertTrue(inbox.alert(self.cfg, "first", todo=todo))
        self.assertTrue(inbox.alert(self.cfg, "second", todo=todo))
        self.assertEqual(len(todo.items), 1)
        row = next(iter(todo.items.values()))
        self.assertEqual((row["title"], row["waiting"]), (inbox.ALERT_TITLE + self.root.name, "second"))
        with patch.object(inbox, "todo_cli", todo):
            self.assertEqual(idle.notify(self.cfg, "third"), "disabled")
        self.assertEqual(row["waiting"], "third")
        with patch.object(inbox, "todo_cli", side_effect=RuntimeError("no node")):
            self.assertEqual(idle.notify(self.cfg, "fourth"), "disabled")

    def test_watcher_exits_when_no_item_is_open_and_survives_poll_errors(self):
        todo = FakeTodo()
        inbox.open_item(self.cfg, self.spec(), todo=todo)
        cycles = []

        def poller(cfg):
            cycles.append(1)
            if len(cycles) == 1:
                raise RuntimeError("to-do unavailable")
            return self.poll(todo)

        def sleep(_):
            item = next(iter(todo.items))
            if todo.items[item]["status"] == "waiting":
                todo.reply(item, "1")
        self.assertEqual(inbox.watch(self.cfg, sleep=sleep, poller=poller), 0)
        self.assertEqual(len(cycles), 2)
        self.assertFalse(inbox.open_items(self.cfg))


class HtmlSurface(Base):
    surface = "html"

    def test_html_never_touches_the_todo_list(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("to-do touched")
        with patch.object(inbox, "todo_cli", forbidden), patch.object(inbox.subprocess, "Popen", forbidden):
            self.assertFalse(inbox.alert(self.cfg, "x"))
            self.assertEqual(idle.notify(self.cfg, "x"), "disabled")
            self.assertFalse(inbox.start_watcher(self.cfg))
            self.assertEqual(inbox.poll(self.cfg), [])

    def test_html_request_still_starts_a_page_server(self):
        with patch.object(dp.subprocess, "Popen") as popen, \
                patch.object(inbox, "open_item", side_effect=AssertionError("to-do touched")):
            popen.return_value.poll.return_value = 1
            with patch.object(dp, "load_config", return_value=self.cfg), self.assertRaisesRegex(RuntimeError, "decision server exited"):
                dp.main(["--config", str(self.cfg.path), "approval", "TASK-1", "--open"])
        command = popen.call_args[0][0]
        self.assertIn("--serve", command)
        self.assertIn("--open", command)


class TodoRequest(Base):
    def test_request_on_todo_surface_opens_item_without_server(self):
        todo = FakeTodo()
        with patch.object(inbox, "todo_cli", todo), patch.object(inbox.subprocess, "Popen") as popen, \
                patch.object(dp, "load_config", return_value=self.cfg), patch.object(dp.webbrowser, "open") as browser:
            self.assertEqual(dp.main(["--config", str(self.cfg.path), "approval", "TASK-1", "--open"]), 0)
        browser.assert_not_called()
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(popen.call_args[0][0][-1], "watch")
        self.assertEqual(list((self.cfg.state_dir / "decision-pages").glob("*.url")), [])
        self.assertEqual([row["status"] for row in todo.items.values()], ["waiting"])


@unittest.skipUnless(HAS_KIT, "node or sdlc-kit to-do CLI missing")
class RealTodoCli(Base):
    """Runs the real to-do CLI against a temporary todo.md; human replies are injected as the app writes them."""

    def setUp(self):
        super().setUp()
        self.todo_file = self.root / "todo.md"
        env = patch.dict(os.environ, SDLC_TODO=str(self.todo_file), SDLC_KIT=str(KIT))
        env.start()
        self.addCleanup(env.stop)

    def item(self, item_id):
        return next(r for r in inbox.todo_cli(["list", "--json"]) if r["id"] == item_id)

    def human_reply(self, item_id, words):
        title = self.item(item_id)["title"]
        lines = self.todo_file.read_text().split("\n")
        start = next(i for i, text in enumerate(lines) if text == f"- [ ] {title}")
        end = next((i for i in range(start + 1, len(lines)) if not lines[i].startswith("  ")), len(lines))
        lines.insert(end, f"  - 2026-11-03 10:00 human: reply — {words}")
        lines = [text for i, text in enumerate(lines) if not (start < i < end and text.startswith("  - waiting: "))]
        self.todo_file.write_text("\n".join(lines))

    def test_choice_invalid_then_number_with_real_cli(self):
        code, item = inbox.open_item(self.cfg, self.spec())
        row = self.item(item)
        self.assertEqual(row["status"], "waiting")
        self.assertTrue(row["title"].startswith(f"[decision {code}] TASK-1"))
        self.human_reply(item, "perhaps")
        self.assertEqual(self.poll(inbox.todo_cli), [(code, "invalid")])
        self.assertIn("Could not use 'perhaps'", self.item(item)["waiting"])
        self.human_reply(item, "D1 2")
        self.assertEqual(self.poll(inbox.todo_cli), [(code, "decided")])
        self.assertEqual(self.item(item)["status"], "done")
        self.assertEqual([(e["option"], e["by"]) for e in self.events() if e["event"] == "decision"], [("skip", "todo")])
        self.assertEqual(self.poll(inbox.todo_cli), [])

    def test_sql_approval_and_alert_with_real_cli(self):
        code, item = inbox.open_item(self.cfg, self.spec("sql-run"))
        self.human_reply(item, "승인")
        self.assertEqual(self.poll(inbox.todo_cli), [(code, "decided")])
        self.assertEqual([k for k, _ in self.ran], ["sql-run"])
        inbox.alert(self.cfg, "first")
        inbox.alert(self.cfg, "second")
        alerts = [r for r in inbox.todo_cli(["list", "--json"]) if r["title"].startswith(inbox.ALERT_TITLE)]
        self.assertEqual([(r["status"], r["waiting"]) for r in alerts], [("waiting", "second")])

    def test_cli_surface_command(self):
        self.cfg.path.write_text('project_root = "."\nstate_dir = "state"\n[decisions]\nsurface = "tui"\n')
        result = subprocess.run([sys.executable, "-m", "factory.decision_inbox", "--config", str(self.cfg.path), "surface"],
                                cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.stdout.strip(), "tui", result.stderr)


if __name__ == "__main__":
    unittest.main()
