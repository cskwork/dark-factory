"""Judge (0.9.0): fake models, fake trackers and a fake to-do list only; nothing leaves the machine."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from support import Fixture
from factory.config import Config
from factory import decision_inbox as inbox
from factory import decision_page as dp
from factory import judge
from factory.idle import decide as guard_decide
from factory.runtime import archive_finished

ME, QA = "u-me", "u-qa"
DONE = "QA done"


def verdict(option="b", decision="decide", risk="low", confidence=0.9, **changes):
    return {"decision": decision, "option": option, "risk": risk, "humanNeeded": "", "against": "against",
            "why": "why", "confidence": confidence, **changes}


def comment(cid, author, created, text="text", media=0):
    return {"id": cid, "author": author, "created": created, "text": text, "media": media}


def facts(status="In QA", comments=(), transitions=(), reporter=QA):
    return {"status": status, "me": ME, "summary": "summary", "comments": list(comments),
            "transitions": list(transitions), "reporter": {"id": reporter, "mention": "@" + reporter}}


def moved(at, by=QA, to=DONE):
    return {"to": to, "at": at, "author": by}


class Base(unittest.TestCase):
    surface = "html"
    judge_mode = "on"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FACTORY_JUDGE_MODE", None)
        self.settings = {"mode": self.judge_mode, "command": ["model", "--json-schema", "{schema}"],
                         "promotion": {"qa_done_status": DONE, "stages": ["promotion"],
                                       "facts_cmd": ["facts", "{key}"], "comment_cmd": ["comment", "{key}"]}}
        self.cfg = Config({"project_root": str(self.root), "state_dir": "state", "key_pattern": r"[A-Z]+-\d+",
                           "decisions": {"surface": self.surface,
                                         "approval": {"on_approve": [sys.executable, "-c", ""]}},
                           "judge": self.settings}, self.root / "factory.toml")
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.woken, self.asked = [], []

    def model(self, *answers):
        answers = list(answers)

        def answer(data):
            self.asked.append(data)
            return answers.pop(0)
        return answer

    def choice(self, **changes):
        return dp.create_request(self.cfg, {
            "kind": "choice", "key": "TASK-1", "title": "Pick", "question": "Which?", "recommended": "a",
            "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], **changes})

    def approval(self):
        return dp.create_request(self.cfg, {"kind": "approval", "key": "TASK-1", "title": "Promote"})

    def jsonl(self, name):
        path = self.cfg.state_dir / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def decisions(self):
        return [row for row in self.jsonl("decisions.jsonl") if row.get("event") == "decision"]


class Mode(Base):
    def test_precedence(self):
        self.assertEqual(judge.mode(self.cfg), "on")
        judge.set_mode(self.cfg, "shadow")
        self.assertEqual(judge.mode(self.cfg), "shadow")
        (self.cfg.state_dir / "judge").write_text("maybe\n")
        self.assertEqual(judge.mode(self.cfg), "off")
        os.environ["FACTORY_JUDGE_MODE"] = "on"
        self.assertEqual(judge.mode(self.cfg), "on")
        with self.assertRaises(ValueError):
            judge.set_mode(self.cfg, "maybe")

    def test_missing_section_is_off_whatever_the_file(self):
        cfg = Config({"project_root": str(self.root), "state_dir": "state"}, self.root / "factory.toml")
        (cfg.state_dir / "judge").write_text("on\n")
        os.environ["FACTORY_JUDGE_MODE"] = "on"
        self.assertEqual(judge.mode(cfg), "off")
        self.assertFalse(judge.applies(cfg, {"kind": "choice"}))

    def test_section_without_mode_is_off_and_config_is_validated(self):
        self.settings.pop("mode")
        self.assertEqual(judge.mode(self.cfg), "off")
        for broken in ({"mode": "on"}, {"command": ["m"], "mode": "always"}, {"command": ["m"], "min_confidence": 2},
                       {"command": ["m"], "promotion": {"qa_done_status": DONE}},
                       {"command": ["m"], "promotion": {"qa_done_status": DONE, "facts_cmd": ["f"], "comment_cmd": ["c"],
                                                         "nudge_template": "{nobody}"}}):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                Config({"judge": broken}, self.root / "f.toml")


class Model(Base):
    def run(self, stdout, code=0):
        return lambda command, **kwargs: (self.asked.append((command, kwargs["input"]))
                                          or SimpleNamespace(returncode=code, stdout=stdout, stderr=""))

    def test_raw_and_envelope_verdicts_and_schema_placeholder(self):
        raw = judge.ask(self.cfg, {"k": 1}, runner=self.run(json.dumps(verdict())))
        self.assertEqual(raw["option"], "b")
        command, prompt = self.asked[0]
        self.assertEqual(json.loads(command[-1]), judge.VERDICT)
        self.assertIn("confidence >= 0.8", prompt)
        envelope = judge.ask(self.cfg, {}, runner=self.run(json.dumps({"structured_output": verdict("a")})))
        self.assertEqual(envelope["option"], "a")

    def test_failures_escalate(self):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("model", 1)
        for runner in (self.run("not json"), self.run(json.dumps(verdict()), code=1), timeout,
                       self.run(json.dumps({"is_error": True, "structured_output": verdict()})),
                       self.run(json.dumps({"decision": "decide"}))):
            with self.subTest(runner=runner):
                result = judge.ask(self.cfg, {}, runner=runner)
                self.assertEqual(result["decision"], "escalate")
                self.assertTrue(result["humanNeeded"].startswith("judge-failed:"))


class Choice(Base):
    def test_decides_records_by_judge_and_wakes(self):
        spec = self.choice()
        self.assertTrue(judge.applies(self.cfg, spec))
        self.assertEqual(judge.choice(self.cfg, spec, model=self.model(verdict("b")), wake=self.woken.append), judge.APPROVED)
        [row] = self.decisions()
        self.assertEqual((row["option"], row["by"], row["messageId"], row["decision_id"]),
                         ("b", "judge", "judge:" + spec["decision_id"], spec["decision_id"]))
        self.assertEqual(self.woken, ["TASK-1"])
        self.assertEqual(self.asked[0]["options"], spec["options"])
        self.assertTrue(self.jsonl("judge.jsonl")[0]["applied"])

    def test_history_from_same_key_is_given(self):
        earlier = self.choice(title="Earlier")
        dp.decide(self.cfg, earlier, "a", wake=self.woken.append, by="button")
        judge.choice(self.cfg, self.choice(), model=self.model(verdict("a", confidence=0.1)), wake=self.woken.append)
        self.assertEqual([(row["title"], row["option"], row["recommended"]) for row in self.asked[0]["history"]],
                         [("Earlier", "a", "a")])

    def test_human_only_never_reaches_the_model(self):
        spec = self.choice(humanReason="prod-data")
        self.assertFalse(judge.applies(self.cfg, spec))
        self.assertEqual(judge.choice(self.cfg, spec, model=self.model()), judge.ESCALATE)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.jsonl("judge.jsonl")[0]["verdict"], "human-only")
        self.assertFalse(judge.applies(self.cfg, self.approval()))

    def test_shadow_low_confidence_unknown_option_and_failure_escalate(self):
        cases = (("shadow", verdict("b")), ("on", verdict("b", confidence=0.79)), ("on", verdict("z")),
                 ("on", verdict("b", risk="high")), ("on", judge._escalation("judge-failed:ValueError")))
        for current, answer in cases:
            with self.subTest(mode=current, answer=answer):
                judge.set_mode(self.cfg, current)
                self.assertEqual(judge.choice(self.cfg, self.choice(), model=self.model(answer), wake=self.woken.append),
                                 judge.ESCALATE)
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.woken, [])
        self.assertEqual(len(self.jsonl("judge.jsonl")), len(cases))

    def test_detached_decided_does_not_open_a_human_item(self):
        spec, opened = self.choice(), []
        result = judge.choice_detached(self.cfg, spec, model=self.model(verdict("a")), wake=self.woken.append,
                                       human=lambda cfg, spec, **kw: opened.append(spec))
        self.assertEqual((result, opened), (judge.APPROVED, []))
        self.assertEqual(dp.decide(self.cfg, spec, "b", wake=self.woken.append)["status"], "used")

    def test_detached_escalation_and_errors_open_the_same_id(self):
        def broken(data):
            raise RuntimeError("model crashed")
        for model in (self.model(verdict("b", confidence=0.1)), broken):
            with self.subTest(model=model):
                spec, opened = self.choice(), []
                result = judge.choice_detached(self.cfg, spec, model=model, wake=self.woken.append,
                                               human=lambda cfg, spec, **kw: opened.append(spec["decision_id"]))
                self.assertEqual((result, opened), (judge.ESCALATE, [spec["decision_id"]]))
        self.assertEqual(self.decisions(), [])

    def test_decision_request_hands_choices_to_the_judge(self):
        with patch.object(judge, "launch_choice") as launch, patch("builtins.print") as shown, \
                patch("factory.decision_page.load_config", return_value=self.cfg):
            self.assertEqual(dp.main(["choice", "TASK-1", "--options-json", '[{"id":"a","label":"A"}]']), 0)
        spec = launch.call_args.args[1]
        self.assertTrue((self.cfg.state_dir / "decisions" / (spec["decision_id"] + ".request.json")).exists())
        self.assertIn("judging " + spec["decision_id"], shown.call_args.args[0])


class TodoEscalation(Base):
    surface = "app"

    def test_escalation_opens_one_todo_item_under_the_same_id(self):
        from test_decision_inbox import FakeTodo
        todo, spec = FakeTodo(), self.choice()
        with patch.object(inbox, "todo_cli", todo), patch.object(inbox, "start_watcher") as watcher:
            judge.choice_detached(self.cfg, spec, model=self.model(verdict("b", decision="escalate")))
        registry = json.loads((self.cfg.state_dir / "decision-pages/todo.json").read_text())
        self.assertEqual(list(registry), [spec["decision_id"]])
        self.assertEqual(len(todo.items), 1)
        watcher.assert_called_once()


class Promotion(Base):
    RESULT = comment("10", ME, "2026-11-09T09:00:00+00:00", media=1)

    def run_promotion(self, data, *answers, posted=None):
        self.posted = [] if posted is None else posted
        return judge.promotion(self.cfg, self.approval(), facts=lambda key: data, model=self.model(*answers),
                               comment=lambda key, text: self.posted.append(text) or f"c{len(self.posted)}",
                               wake=self.woken.append)

    def test_other_user_transition_after_result_approves(self):
        data = facts(DONE, [self.RESULT], [moved("2026-11-09T10:00:00+00:00")])
        self.assertEqual(self.run_promotion(data), judge.APPROVED)
        [row] = self.decisions()
        self.assertEqual((row["kind"], row["option"], row["by"]), ("approval", "approve", "judge"))
        self.assertTrue(json.loads((self.cfg.state_dir / "approvals.json").read_text())["TASK-1"]["approved"])
        self.assertEqual((self.woken, self.asked, self.posted), (["TASK-1"], [], []))

    def test_shadow_still_applies_the_deterministic_rule(self):
        judge.set_mode(self.cfg, "shadow")
        data = facts(DONE, [self.RESULT], [moved("2026-11-09T10:00:00Z")])
        self.assertEqual(self.run_promotion(data), judge.APPROVED)

    def test_self_transition_self_report_no_result_and_errors_go_to_a_human(self):
        cases = (facts(DONE, [self.RESULT], [moved("2026-11-09T10:00:00+00:00", by=ME)]),
                 facts("In QA", [self.RESULT], reporter=ME),
                 facts(DONE, [comment("9", ME, "2026-11-09T09:00:00+00:00")], [moved("2026-11-09T10:00:00+00:00")]))
        for data in cases:
            with self.subTest(data=data):
                self.assertEqual(self.run_promotion(data), judge.ESCALATE)
        self.assertEqual(judge.promotion(self.cfg, self.approval(), facts=lambda key: {"bad": True}), judge.ESCALATE)
        self.assertEqual((self.decisions(), self.posted, self.asked), ([], [], []))

    def test_transition_before_newer_result_nudges_once_per_result(self):
        posted = []
        data = facts(DONE, [self.RESULT], [moved("2026-11-09T08:00:00+00:00")])
        self.assertEqual(self.run_promotion(data, posted=posted), judge.WAITING)
        self.assertEqual(self.run_promotion(data, posted=posted), judge.WAITING)
        self.assertEqual(len(posted), 1)
        self.assertIn("@" + QA, posted[0])
        self.assertIn(f"'{DONE}'", posted[0])
        data["comments"].append(comment("11", ME, "2026-11-09T11:00:00+00:00", media=2))
        self.assertEqual(self.run_promotion(data, posted=posted), judge.WAITING)
        self.assertEqual(len(posted), 2)
        state = json.loads((self.cfg.state_dir / "judge-state.json").read_text())
        self.assertEqual(state["promotionAsked"]["TASK-1"]["resultId"], "11")
        self.assertEqual(state["promotionSeen"]["TASK-1"], DONE)

    def test_others_ok_comment_approves(self):
        data = facts("In QA", [self.RESULT, comment("12", QA, "2026-11-09T10:00:00+00:00", "Looks good")])
        self.assertEqual(self.run_promotion(data, verdict("approve")), judge.APPROVED)
        self.assertEqual(self.asked[0]["comments"][0]["text"], "Looks good")
        self.assertEqual(self.decisions()[0]["by"], "judge")

    def test_defect_comment_waits_without_nudge(self):
        data = facts("In QA", [self.RESULT, comment("12", QA, "2026-11-09T10:00:00+00:00", "Broken on save")])
        self.assertEqual(self.run_promotion(data, verdict("approve", decision="escalate", risk="high")), judge.WAITING)
        self.assertEqual((self.posted, self.decisions()), ([], []))

    def test_judge_failure_goes_to_a_human(self):
        data = facts("In QA", [self.RESULT, comment("12", QA, "2026-11-09T10:00:00+00:00", "OK")])
        self.assertEqual(self.run_promotion(data, judge._escalation("judge-failed:TimeoutExpired")), judge.ESCALATE)
        self.assertEqual(self.posted, [])

    def test_comment_adapter_reads_back(self):
        store = self.root / "facts.json"
        store.write_text(json.dumps(facts("In QA", [self.RESULT])))
        script = self.root / "tracker.py"
        script.write_text("""import json, sys
path = sys.argv[1]
data = json.load(open(path))
if sys.argv[2] == "comment":
    data["comments"].append({"id": "77", "author": "%s", "created": "2026-11-09T12:00:00+00:00",
                             "text": sys.stdin.read(), "media": 0})
    json.dump(data, open(path, "w"))
    print("77")
else:
    print(json.dumps(data))
""" % ME)
        self.settings["promotion"].update(facts_cmd=[sys.executable, str(script), str(store), "facts", "{key}"],
                                          comment_cmd=[sys.executable, str(script), str(store), "comment", "{key}"])
        self.assertEqual(judge.promotion(self.cfg, self.approval()), judge.WAITING)
        self.assertIn("@" + QA, json.loads(store.read_text())["comments"][-1]["text"])
        self.assertEqual(self.jsonl("judge.jsonl")[-1]["commentId"], "77")
        self.settings["promotion"]["comment_cmd"] = [sys.executable, "-c", "print('88')"]
        (self.cfg.state_dir / "judge-state.json").unlink()
        self.assertEqual(judge.promotion(self.cfg, self.approval()), judge.ESCALATE)  # 88 never reads back

    def test_decision_request_exit_codes(self):
        cases = ((judge.APPROVED, 0), (judge.WAITING, 11))
        for code, expected in cases:
            with self.subTest(code=code), patch.object(judge, "promotion", return_value=code), \
                    patch("builtins.print"), patch("factory.decision_page.load_config", return_value=self.cfg):
                self.assertEqual(dp.main(["approval", "TASK-1"]), expected)


class HumanRisk(Base):
    def queue(self, question="Retry the flaky check?", suggest="Retry once", **item):
        value = {"stage": "build", "waiting": "human-risk", "note": "work", "question": question, "suggest": suggest, **item}
        (self.cfg.state_dir / "queue.json").write_text(json.dumps({"TASK-1": value}))

    def item(self):
        return json.loads((self.cfg.state_dir / "queue.json").read_text())["TASK-1"]

    def test_accept_resumes_and_records(self):
        self.queue()
        self.assertEqual(judge.risk(self.cfg, model=self.model(verdict("accept"))), 1)
        item = self.item()
        self.assertIsNone(item["waiting"])
        self.assertNotIn("question", item)
        self.assertNotIn("suggest", item)
        self.assertRegex(item["note"], r"^work \| \[.+ judge\] instead of a human: proceed with 'Retry once'")
        [row] = self.decisions()
        self.assertEqual((row["kind"], row["option"], row["by"]), ("human-risk", "accept", "judge"))
        self.assertEqual(self.asked[0]["options"], [{"id": "accept", "label": "Retry once"}])

    def test_same_question_once_and_daily_cap(self):
        self.queue()
        self.assertEqual(judge.risk(self.cfg, model=self.model(verdict("accept", decision="escalate"))), 1)
        self.assertEqual(judge.risk(self.cfg, model=self.model()), 0)
        self.assertEqual(self.item()["waiting"], "human-risk")
        for question in ("Second?", "Third?"):
            self.queue(question=question)
            judge.risk(self.cfg, model=self.model(verdict("accept")))
        self.queue(question="Fourth?")
        self.assertEqual(judge.risk(self.cfg, model=self.model()), 0)
        self.assertEqual(len(self.asked), 3)

    def test_items_without_question_leased_keys_and_shadow_are_left(self):
        self.queue(suggest="")
        self.assertEqual(judge.risk(self.cfg, model=self.model()), 0)
        self.queue()
        self.assertEqual(judge.risk(self.cfg, model=self.model(), blocked={"TASK-1"}), 0)
        self.assertEqual(judge.risk(self.cfg, model=self.model(), blocked={"*"}), 0)
        judge.set_mode(self.cfg, "shadow")
        self.assertEqual(judge.risk(self.cfg, model=self.model(verdict("accept"))), 1)
        self.assertEqual(self.item()["waiting"], "human-risk")
        self.assertEqual(self.decisions(), [])

    def test_concurrent_change_is_never_overwritten(self):
        self.queue()

        def model(data):
            self.queue(note="changed by a worker")
            return verdict("accept")
        judge.risk(self.cfg, model=model)
        self.assertEqual((self.item()["waiting"], self.item()["note"]), ("human-risk", "changed by a worker"))
        self.assertEqual(self.decisions(), [])
        self.assertFalse(self.jsonl("judge.jsonl")[0]["applied"])


class Guard(Fixture):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"FACTORY_JUDGE_MODE": "on"})
        env.start()
        self.addCleanup(env.stop)
        self.data["judge"] = {"mode": "on", "command": ["model"],
                              "promotion": {"qa_done_status": DONE, "stages": ["promotion"],
                                            "facts_cmd": ["facts", "{key}"], "comment_cmd": ["comment", "{key}"]}}

    def statuses(self, **statuses):
        self.responses["scan"] = {"stdout": json.dumps({"events": [], "errors": [], "queueStatuses": statuses})}
        self.save_responses()

    def test_reporter_wait_resumes_on_qa_done_status(self):
        self.write("queue.json", {"PROJ-1": {"stage": "promotion", "waiting": "reporter", "note": "asked"},
                                  "PROJ-2": {"stage": "build", "waiting": "reporter"}})
        self.statuses(**{"PROJ-1": DONE, "PROJ-2": DONE})
        result, code = guard_decide(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("queue-runnable:PROJ-1", result["reasons"])
        self.assertEqual(result["queueReconcile"]["promotion"], {"PROJ-1": DONE})
        queue = self.read("queue.json")
        self.assertIsNone(queue["PROJ-1"]["waiting"])
        self.assertIn("promotion resumes", queue["PROJ-1"]["note"])
        self.assertEqual(queue["PROJ-2"]["waiting"], "reporter")

    def test_status_the_judge_already_saw_does_not_resume(self):
        self.write("queue.json", {"PROJ-1": {"stage": "promotion", "waiting": "reporter"}})
        self.write("judge-state.json", {"promotionSeen": {"PROJ-1": DONE}})
        self.statuses(**{"PROJ-1": DONE})
        result, code = guard_decide(self.cfg)
        self.assertEqual((code, self.read("queue.json")["PROJ-1"]["waiting"]), (0, "reporter"))
        os.environ["FACTORY_JUDGE_MODE"] = "off"
        self.write("judge-state.json", {})
        self.assertEqual(guard_decide(self.cfg)[1], 0)

    def test_guard_launches_risk_judging_only_for_candidates(self):
        self.write("queue.json", {"PROJ-1": {"waiting": "human-risk", "question": "Retry?", "suggest": "Retry"}})
        with patch.object(judge, "launch_risk") as launch:
            result, _ = guard_decide(self.cfg)
        launch.assert_called_once_with(self.cfg)
        self.assertEqual(result["judge"], "risk-launched")
        self.write("queue.json", {"PROJ-1": {"waiting": "human-risk"}})
        with patch.object(judge, "launch_risk") as launch:
            guard_decide(self.cfg)
        launch.assert_not_called()

    def test_unused_approval_waits_for_the_environment(self):
        self.write("queue.json", {"PROJ-1": {"stage": "promotion", "waiting": "env-human", "needs": "network"}})
        self.write("approvals.json", {"PROJ-1": {"approved": True, "used_at": None}})
        with patch("factory.preflight.check", return_value=({"results": []}, 10)):
            result, code = guard_decide(self.cfg)
        self.assertEqual(code, 0)
        self.assertNotIn("approval-unused:PROJ-1", result["reasons"])
        with patch("factory.preflight.check", return_value=({"results": []}, 0)):
            result, code = guard_decide(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("environment-ready:PROJ-1", result["reasons"])


class Archive(Fixture):
    def setUp(self):
        super().setUp()
        self.data["launcher"].update({"list_cmd": ["injected", "list", "{token}"],
                                      "archive_cmd": ["injected", "archive", "{id}"]})
        self.rows, self.operations, self.broken = [], [], set()

        def run(command, **values):
            argv = self.cfg.command(command, **values)
            self.operations.append(argv)
            if argv[1] == "archive" and argv[2] in self.broken:
                raise OSError("archive failed")
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.rows) if argv[1] == "list" else "", "")
        adapter = patch.object(self.cfg, "run", side_effect=run)
        adapter.start()
        self.addCleanup(adapter.stop)
        (self.state / "runs/old").mkdir(parents=True)
        (self.state / "runs/old/ended").touch()

    def lists(self):
        return sum(op[1] == "list" for op in self.operations)

    def test_settled_run_is_marked_and_not_listed_again(self):
        self.rows = [{"id": "w1", "status": "idle"}]
        self.assertEqual(archive_finished(self.cfg), (1, 0))
        self.assertTrue((self.state / "runs/old/archived").exists())
        self.assertEqual(archive_finished(self.cfg), (0, 0))
        self.assertEqual(self.lists(), 1)

    def test_running_or_failed_archive_keeps_the_run(self):
        for rows, broken in (([{"id": "w1", "status": "running"}], set()), ([{"id": "w2", "status": "idle"}], {"w2"})):
            with self.subTest(rows=rows):
                self.rows, self.broken = rows, broken
                archive_finished(self.cfg)
                self.assertFalse((self.state / "runs/old/archived").exists())


class FakeTodo:
    def __init__(self, rows=(), missing=()):
        self.rows, self.calls, self.missing = list(rows), [], set(missing)

    def __call__(self, args):
        self.calls.append(args)
        if args[0] in self.missing:
            raise RuntimeError("unknown command: " + args[0])
        if args[0] == "list":
            return self.rows
        return "t1" if args[0] == "add" else ""


class Setting(Base):
    surface = "app"

    def setUp(self):
        super().setUp()
        self.retitled = []

    def row(self, *replies, current="on", **changes):
        return {"id": "t1", "title": inbox.setting_title(self.cfg, current), "status": "others",
                "others": inbox.setting_others(current), "pin": True,
                "replies": [{"who": "human", "at": f"2026-11-09T10:0{i}", "what": "reply — " + words}
                            for i, words in enumerate(replies)], **changes}

    def setting(self, todo, rows):
        return inbox.judge_setting(self.cfg, rows, todo=todo, item_cli=self.retitled.append)

    def test_creates_pinned_others_item_once(self):
        todo = FakeTodo()
        self.assertEqual(self.setting(todo, []), "on")
        self.assertEqual(todo.calls[0][:2], ["add", "[factory] judge: on"])
        self.assertEqual(todo.calls[1], ["choices", "t1", "on | off", "--pick", "1"])
        self.assertEqual(todo.calls[-2:], [["others", "t1", "setting · judge on · reply on or off"], ["pin", "t1", "on"]])
        todo = FakeTodo()
        self.setting(todo, [self.row()])
        self.assertEqual((todo.calls, self.retitled), ([], []))

    def test_reply_toggles_once_and_retitles(self):
        todo = FakeTodo()
        self.assertEqual(self.setting(todo, [self.row("off")]), "off")
        self.assertEqual(judge.mode(self.cfg), "off")
        self.assertIn(["choices", "t1", "on | off", "--pick", "2"], todo.calls)
        self.assertIn(["others", "t1", "setting · judge off · reply on or off"], todo.calls)
        self.assertIn(["take", "t1", "--json"], todo.calls)
        self.assertEqual(self.retitled, [["retitle", "t1", "[factory] judge: off"]])
        # The same reply, now under a new id after the retitle, is not applied again.
        judge.set_mode(self.cfg, "on")
        self.setting(FakeTodo(), [self.row("off", current="on", id="t2")])
        self.assertEqual(judge.mode(self.cfg), "on")

    def test_unknown_reply_is_explained_and_old_kit_without_pin_tolerated(self):
        todo = FakeTodo(missing={"pin", "take"})
        self.assertEqual(self.setting(todo, [self.row("maybe", pin=False)]), "on")
        self.assertIn("Could not use 'maybe'", todo.calls[1][2])
        self.assertEqual(judge.mode(self.cfg), "on")
        self.assertEqual(self.setting(FakeTodo(missing={"pin"}), []), "on")

    def test_poll_keeps_the_setting_and_html_or_unconfigured_does_nothing(self):
        todo = FakeTodo()
        self.assertEqual(inbox.poll(self.cfg, todo=todo, item_cli=self.retitled.append), [])
        self.assertEqual([call[0] for call in todo.calls][:2], ["list", "add"])
        self.assertTrue(inbox.setting_enabled(self.cfg))
        self.cfg.data["judge"] = {}
        self.assertFalse(inbox.setting_enabled(self.cfg))
        self.cfg.data["judge"], self.cfg.data["decisions"]["surface"] = self.settings, "html"
        self.assertIsNone(inbox.judge_setting(self.cfg, [], todo=FakeTodo()))

    def test_setting_failure_does_not_block_decisions(self):
        todo = FakeTodo(missing={"add"})
        with patch("sys.stderr"):
            self.assertEqual(inbox.poll(self.cfg, todo=todo), [])


if __name__ == "__main__":
    unittest.main()
