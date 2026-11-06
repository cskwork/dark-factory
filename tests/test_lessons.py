"""Lesson recurrence uses existing decision requests and human answer audit rows."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import contextlib
import importlib
import io
import json
import subprocess
import sys
from unittest.mock import Mock, patch

from factory import decision_page, state
from support import Fixture, ROOT


class LessonTests(Fixture):
    def setUp(self):
        super().setUp()
        self.lessons = importlib.import_module("factory.lessons")
        self.now = datetime(2026, 11, 6, tzinfo=timezone.utc)
        self.opened = []
        self.opener = Mock(side_effect=self.open_decision)

    def open_decision(self, cfg, path):
        spec = decision_page.create_request(cfg, json.loads(path.read_text()))
        self.opened.append(spec)
        return spec

    def add(self, days=0, **changes):
        row = {"kind": "infra_fail", "what": "PROJ-123 gate failed commentId=123",
               "at": (self.now - timedelta(days=days)).isoformat(), "evidence": "runs/gate.json"}
        row.update(changes)
        return self.lessons.append_lesson(self.cfg, row, now=self.now, opener=self.opener)

    def test_signature_strips_identifiers_but_preserves_failure_and_kind(self):
        first = {"kind": "infra_fail", "what": "PROJ-123 gate exit 4 2026-11-05T09:25:00+09:00 SHA abcdef123 commentId=note-one"}
        second = {"kind": "infra_fail", "what": "TASK-456 gate exit 7 2026-10-30T16:22:00Z SHA 987fedab1 commentId=note-two"}
        self.assertEqual(self.lessons.signature(first), self.lessons.signature(second))
        self.assertNotEqual(self.lessons.signature(first), self.lessons.signature(dict(second, kind="guard")))
        self.assertNotEqual(self.lessons.signature(first), self.lessons.signature(dict(second, what="different failure")))

    def test_third_occurrence_opens_existing_request_with_recommendation_and_evidence(self):
        self.add(days=13)
        self.add(days=1)
        self.assertEqual(self.opened, [])
        result = self.add()
        self.assertEqual((result["count"], result["decision"]), (3, "opened"))
        spec = self.opened[0]
        self.assertEqual(spec["recommended"], "fix-now")
        self.assertEqual([o["id"] for o in spec["options"]], ["fix-now", "accept", "ignore"])
        self.assertIn("3 occurrences", spec["summary"])
        self.assertIn("2026-10-24", spec["summary"])
        self.assertIn("2026-11-06", spec["summary"])
        self.assertEqual(spec["evidence"], ["runs/gate.json"])
        self.assertEqual(len(list((self.state / "decisions").glob("*.request.json"))), 1)

    def test_open_request_deduplicates_and_all_rows_remain(self):
        for _ in range(5):
            result = self.add()
        self.assertEqual(result["decision"], "already-open")
        self.assertEqual(self.opener.call_count, 1)
        self.assertEqual(len((self.state / "lessons.jsonl").read_text().splitlines()), 5)

    def test_concurrent_threads_open_one_request(self):
        self.add()
        self.add()
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: self.add(), range(4)))
        self.assertEqual(self.opener.call_count, 1)
        self.assertEqual(sum(r["decision"] == "opened" for r in results), 1)

    def test_concurrent_processes_append_every_row_and_open_one_request(self):
        self.save_config()
        program = '''import json, sys
from factory.config import load_config
from factory.lessons import append_lesson
from factory.decision_page import create_request
cfg = load_config(sys.argv[1])
def opener(cfg, path):
    create_request(cfg, json.loads(path.read_text()))
for _ in range(4):
    append_lesson(cfg, {"kind": "infra_fail", "what": "PROJ-123 repeated gate failure"}, opener=opener)
'''
        children = [subprocess.Popen([sys.executable, "-c", program, str(self.config)], cwd=ROOT,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
        for child in children:
            out, err = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, out + err)
        self.assertEqual(len((self.state / "lessons.jsonl").read_text().splitlines()), 12)
        self.assertEqual(len(list((self.state / "decisions").glob("*.request.json"))), 1)

    def test_window_excludes_old_and_future_rows_but_includes_boundary(self):
        self.add(days=15)
        self.add(days=-1)
        self.add(days=14)
        self.assertEqual(self.add()["count"], 2)
        self.assertEqual(self.opener.call_count, 0)
        self.assertEqual(self.add()["decision"], "opened")

    def test_real_answer_starts_cooldown_from_answer_not_opening(self):
        for _ in range(3):
            self.add()
        with patch.object(decision_page, "_stamp", return_value=self.now.isoformat()):
            decision_page.decide(self.cfg, self.opened[0], "accept", wake=lambda key: None)
        self.now += timedelta(days=6, hours=23)
        self.assertEqual(self.add()["decision"], "answer-cooldown")
        self.now += timedelta(hours=1)
        self.assertEqual(self.add()["decision"], "opened")
        self.assertEqual(self.opener.call_count, 2)

    def test_claim_without_audit_does_not_duplicate_inflight_decision(self):
        for _ in range(3):
            self.add()
        (self.state / "decisions" / (self.opened[0]["decision_id"] + ".claimed")).write_text(self.now.isoformat())
        self.assertEqual(self.add()["decision"], "already-open")

    def test_configurable_count_window_and_cooldown(self):
        self.data["lessons"] = {"escalate_count": 2, "window_days": 2, "cooldown_days": 1}
        self.add(days=3)
        self.assertEqual(self.add()["count"], 1)
        self.assertEqual(self.add()["decision"], "opened")
        with patch.object(decision_page, "_stamp", return_value=self.now.isoformat()):
            decision_page.decide(self.cfg, self.opened[0], "ignore", wake=lambda key: None)
        self.now += timedelta(days=1)
        self.assertEqual(self.add()["decision"], "opened")

    def test_legacy_observation_epoch_rows_count_without_rewriting_history(self):
        legacy = {"kind": "infra_fail", "observation": "PROJ-456 gate failed commentId=old",
                  "at": self.now.timestamp(), "key": None}
        original = json.dumps(legacy) + "\n"
        path = self.state / "lessons.jsonl"
        path.write_text(original * 2)
        self.assertEqual(self.add()["decision"], "opened")
        self.assertTrue(path.read_text().startswith(original * 2))

    def test_epoch_now_and_global_lesson_without_ticket(self):
        row = {"kind": "infra_fail", "observation": "Intake scan failed", "at": self.now.timestamp(), "key": None}
        for _ in range(3):
            result = self.lessons.append_lesson(self.cfg, row, now=self.now.timestamp(), opener=self.opener)
        self.assertEqual(result["decision"], "opened")
        self.assertTrue(self.cfg.valid_key(self.opened[0]["key"]))
        self.assertEqual(json.loads((self.state / "lessons.jsonl").read_text().splitlines()[0])["what"], row["observation"])

    def test_invalid_row_does_not_append(self):
        for row in ({"what": "missing kind"}, {"kind": "infra_fail", "what": " "}, [],
                    {"kind": "infra_fail", "what": "bad time", "at": "not a date"}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.lessons.append_lesson(self.cfg, row, opener=self.opener)
        self.assertFalse((self.state / "lessons.jsonl").exists())

    def test_open_failure_is_explicit_after_append(self):
        self.add()
        self.add()
        self.opener.side_effect = OSError("decision surface unavailable")
        with self.assertRaisesRegex(OSError, "surface unavailable"):
            self.add()
        self.assertEqual(len((self.state / "lessons.jsonl").read_text().splitlines()), 3)
        failure = json.loads(next((self.state / "lesson-decisions").glob("*.error.json")).read_text())
        self.assertEqual(failure["error"], "OSError: decision surface unavailable")

    def test_custom_key_pattern_failure_keeps_global_lesson_and_diagnostic(self):
        self.data["key_pattern"] = r"TASK-\d+"
        for _ in range(2):
            self.add(what="Intake scan failed", key=None)
        with self.assertRaisesRegex(ValueError, "decision key"):
            self.add(what="Intake scan failed", key=None)
        self.assertEqual(len((self.state / "lessons.jsonl").read_text().splitlines()), 3)
        self.assertEqual(self.opener.call_count, 0)
        self.assertEqual(len(list((self.state / "lesson-decisions").glob("*.error.json"))), 1)

    def test_closed_todo_request_does_not_suppress_future_recurrences(self):
        for _ in range(3):
            self.add()
        self.write("decision-pages/todo.json", {self.opened[0]["decision_id"]: {"state": "held", "itemDone": True}})
        self.assertEqual(self.add()["decision"], "opened")
        self.assertEqual(self.opener.call_count, 2)

    def test_opener_uses_existing_cli_with_config_and_requested_surface(self):
        path = self.write("lesson-decisions/request.json", {"kind": "choice", "key": "PROJ-123"})
        with patch.object(self.lessons.subprocess, "run") as run:
            self.lessons.open_request(self.cfg, path)
        args = run.call_args.args[0]
        self.assertEqual(args[1:], ["--config", str(self.config), "choice", "PROJ-123", "--spec", str(path), "--open"])
        self.assertTrue(args[0].endswith("/bin/decision-request"))
        self.assertTrue(run.call_args.kwargs["check"])

    def test_cli_escalation_uses_same_writer(self):
        self.now = datetime.now(timezone.utc)
        self.add()
        self.add()
        self.save_config()
        with patch.object(self.lessons, "open_request", side_effect=self.open_decision), contextlib.redirect_stdout(io.StringIO()) as output:
            code = state.main(["--config", str(self.config), "lesson", "--json", json.dumps({
                "kind": "infra_fail", "what": "PROJ-456 gate failed commentId=new"})])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["decision"], "opened")


class LessonCLI(Fixture):
    def test_cli_records_and_rejects_invalid_json_rows(self):
        self.save_config()
        args = [sys.executable, "-m", "factory.state", "--config", str(self.config), "lesson", "--json"]
        result = subprocess.run(args + [json.dumps({"kind": "guard", "what": "PROJ-123 gate failure"})],
                                cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "not-needed")
        row = json.loads((self.state / "lessons.jsonl").read_text())
        self.assertIn("at", row)
        self.assertIn("signature", row)
        for invalid in ("[]", '{"what":"missing kind"}'):
            result = subprocess.run(args + [invalid], cwd=ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 1)
            self.assertIn("invalid state or arguments", result.stderr)
        self.assertEqual(len((self.state / "lessons.jsonl").read_text().splitlines()), 1)
