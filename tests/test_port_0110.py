"""0.11.0: lesson classes and index, minimal launch environment, queue note cap, runs/ hygiene, dreaming."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
import gzip
import io
import json
import os
import subprocess
import sys
import time
from unittest.mock import patch

from factory import dream, judge, lessons, runtime
from factory.config import Config
from factory.state import NOTE_MAX_CHARS, NOTE_MAX_LINES, note_marker, patch as state_patch
from support import Fixture, ROOT


class LessonClassTests(Fixture):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 11, 16, 9, tzinfo=timezone.utc)

    def add(self, days=0, **row):
        row = {"kind": "infra_fail", "what": "gate failed", "at": (self.now - timedelta(days=days)).isoformat(), **row}
        return lessons.append_lesson(self.cfg, row, now=self.now, opener=lambda cfg, path: None)

    def lines(self):
        return [json.loads(line) for line in (self.state / "lessons.jsonl").read_text().splitlines()]

    def test_rows_without_class_keep_their_old_signature_byte_for_byte(self):
        # Hashes produced by the 0.10.1 signature() for the same rows.
        self.assertEqual(lessons.signature({"kind": "infra_fail", "what": "PROJ-123 gate exit 4 at 2026-11-05T09:25:00Z sha abcdef123"}),
                         "6770dc5e6295c74cb497d655c60ba402de582733b3548e62f712e30be537f62e")
        self.assertEqual(lessons.signature({"kind": "Worker_Fail ", "observation": "Verifier unavailable"}),
                         "401094a7973a938c40a2b0b38c2fbdea5eb992f182668f8b07031703e1468ee1")
        other = {"kind": "infra_fail", "what": "PROJ-123 gate exit 4 at 2026-11-05T09:25:00Z sha abcdef123", "class": "other"}
        self.assertEqual(lessons.signature(other), "6770dc5e6295c74cb497d655c60ba402de582733b3548e62f712e30be537f62e")

    def test_class_groups_different_wording_by_kind_and_class(self):
        first = {"kind": "infra_fail", "what": "deploy job never started", "class": "deploy"}
        second = {"kind": "infra_fail", "what": "checkout commit was stale", "class": "deploy"}
        self.assertEqual(lessons.signature(first), lessons.signature(second))
        self.assertNotEqual(lessons.signature(first), lessons.signature(dict(second, kind="worker_fail")))
        self.assertNotEqual(lessons.signature(first), lessons.signature(dict(second, **{"class": "scope"})))

    def test_three_classed_lessons_with_different_wording_escalate(self):
        for days, what in ((3, "deploy job never started"), (2, "stale checkout"), (0, "wrong build number")):
            result = self.add(days, what=what, **{"class": "deploy"})
        self.assertEqual((result["count"], result["decision"]), (3, "opened"))

    def test_unknown_class_is_rejected_and_default_list_is_optional(self):
        with self.assertRaisesRegex(ValueError, "lesson class must be one of"):
            self.add(**{"class": "made-up"})
        self.assertEqual(self.add()["decision"], "not-needed")
        self.assertNotIn("class", self.lines()[0])

    def test_configured_classes_add_other_and_cli_requires_a_class(self):
        self.data["lessons"] = {"classes": ["flaky-probe"]}
        self.assertEqual(Config(self.data, self.config).lesson_classes(), (["flaky-probe", "other"], True))
        missing = self.cli_state("lesson", "--json", json.dumps({"kind": "infra_fail", "what": "probe flapped"}))
        self.assertEqual(missing.returncode, 1)
        self.assertIn("lesson class must be one of: flaky-probe, other", missing.stderr)
        ok = self.cli_state("lesson", "--json", json.dumps({"kind": "infra_fail", "what": "probe flapped",
                                                            "class": "flaky-probe"}))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(self.lines()[0]["class"], "flaky-probe")

    def test_cli_without_configured_classes_accepts_a_row_without_class(self):
        result = self.cli_state("lesson", "--json", json.dumps({"kind": "infra_fail", "what": "probe flapped"}))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_class_configuration_is_rejected(self):
        for classes in ([], ["Upper"], "deploy", ["ok", ""]):
            with self.subTest(classes=classes), self.assertRaisesRegex(ValueError, "lessons.classes"):
                Config({**self.data, "lessons": {"classes": classes}}, self.config)

    def cli_state(self, *args):
        self.save_config()
        return subprocess.run([sys.executable, "-m", "factory.state", "--config", str(self.config), *args],
                              capture_output=True, text=True, timeout=20, cwd=ROOT,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


class LessonIndexTests(Fixture):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 11, 16, 9, tzinfo=timezone.utc)

    def rows(self, *rows):
        with (self.state / "lessons.jsonl").open("a") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    def at(self, days):
        return (self.now - timedelta(days=days)).isoformat()

    def test_index_groups_by_class_with_count_last_date_suggestion_and_fix(self):
        sig = lessons.signature({"kind": "infra_fail", "class": "deploy"})
        self.write("lesson-decisions/fixes.json", {sig: {"state": "fixed", "commit": "abc1234", "closedAt": self.at(1)}})
        self.rows({"kind": "infra_fail", "class": "deploy", "what": "a", "at": self.at(3), "suggestion": "old idea"},
                  {"kind": "infra_fail", "class": "deploy", "what": "b", "at": self.at(1), "suggestion": "check the build log"},
                  {"kind": "infra_fail", "class": "scope", "what": "c", "at": self.at(2)},
                  {"kind": "infra_fail", "class": "scope", "what": "too old", "at": self.at(20)},
                  {"kind": "worker_fail", "what": "single unclassed", "at": self.at(1)},
                  {"kind": "worker_fail", "what": "repeat 1 unclassed", "at": self.at(2)},
                  {"kind": "worker_fail", "what": "repeat 2 unclassed", "at": self.at(1)})
        text = lessons.index(self.cfg, now=self.now)
        self.assertEqual(text.splitlines(), [
            "- deploy x2 (last 2026-11-15) — check the build log [fix: fixed abc1234]",
            "- repeat 2 unclassed x2 (last 2026-11-15) — no suggestion [fix: none]",
            "- scope x1 (last 2026-11-14) — no suggestion [fix: none]"])

    def test_index_is_bounded_and_filters_by_stage(self):
        self.rows(*({"kind": "k", "class": "other", "what": f"failure {chr(97 + n)} " * 2, "at": self.at(0)}
                    for n in range(26) for _ in range(2)))
        self.rows({"kind": "k", "class": "deploy", "what": "x", "at": self.at(0), "stage": "ship",
                   "suggestion": "y" * 400})
        lines = lessons.index(self.cfg, now=self.now).splitlines()
        self.assertEqual(len(lines), 20)
        self.assertTrue(all(len(line) <= lessons.INDEX_LINE_CHARS for line in lines))
        self.assertEqual(lessons.index(self.cfg, stage="ship", now=self.now).count("\n"), 0)
        self.assertIn("- deploy x1", lessons.index(self.cfg, stage="ship", now=self.now))
        self.assertEqual(lessons.index(self.cfg, stage="build", now=self.now), "")

    def test_cli_index_prints_lines(self):
        self.rows({"kind": "k", "class": "deploy", "what": "x", "at": datetime.now(timezone.utc).isoformat()})
        self.save_config()
        result = subprocess.run([sys.executable, "-m", "factory.lessons", "index", "--config", str(self.config)],
                                capture_output=True, text=True, timeout=20, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("- deploy x1 (last "))

    def test_block_fails_open_on_a_broken_lesson_file(self):
        (self.state / "lessons.jsonl").write_text("{not json\n")
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(lessons.lesson_block(self.cfg), "")
        self.assertIn("index skipped", errors.getvalue())

    def test_brief_stage_is_explicit_and_unambiguous(self):
        self.assertEqual(lessons.brief_stage("Goal: fix\nstage: review\n"), "review")
        self.assertIsNone(lessons.brief_stage("The review stage starts later."))
        self.assertIsNone(lessons.brief_stage("stage: build\nstage: review"))

    def test_orchestrator_and_worker_prompts_carry_the_block(self):
        now = datetime.now(timezone.utc).isoformat()
        self.rows({"kind": "k", "class": "deploy", "what": "x", "at": now, "stage": "review", "suggestion": "look first"},
                  {"kind": "k", "class": "scope", "what": "y", "at": now, "stage": "build", "suggestion": "stay in scope"})
        with redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.wake(self.cfg, manual=True), 0)
        prompt = self.calls()[-1][-1]
        self.assertIn("Recent lessons (last 14 days", prompt)
        self.assertIn("- deploy x1", prompt)
        self.assertIn("- scope x1", prompt)
        brief = self.root / "brief.md"
        brief.write_text("stage: review\nDo the review.")
        result = {"status": "done", "summary": "ok", "key": None, "stage": "review", "waiting": None,
                  "artifacts": [], "checks": []}
        self.responses["launch"] = {"stdout": json.dumps(result)}
        self.save_responses()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.worker(self.cfg, "builder", "Unit", str(brief)), 0)
        prompt = self.calls()[-1][-1]
        self.assertIn("- deploy x1", prompt)
        self.assertNotIn("- scope x1", prompt)
        brief.write_text("Do the review.")
        with redirect_stdout(io.StringIO()):
            runtime.worker(self.cfg, "builder", "Unit", str(brief))
        self.assertNotIn("Recent lessons", self.calls()[-1][-1])


class LaunchEnvironmentTests(Fixture):
    def setUp(self):
        super().setUp()
        self.data["launcher"]["env_args"] = ["--env", "{name}={value}"]
        self.data["launch"] = {"env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "SHARED": "launch"}}
        self.data["worker"] = {"env": {"SHARED": "worker"}}
        self.cfg = Config(self.data, self.config)

    def wake_call(self):
        with patch("factory.runtime.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, "owner", "")) as run, redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.wake(self.cfg, manual=True), 0)
        return run.call_args

    def test_orchestrator_launch_gets_flags_and_process_environment(self):
        call = self.wake_call()
        self.assertIn("CLAUDE_CODE_DISABLE_AUTO_MEMORY=1", call.args[0])
        self.assertEqual(call.kwargs["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")

    def test_worker_gets_launch_environment_and_worker_env_wins(self):
        brief = self.root / "brief.md"
        brief.write_text("Do it.")
        result = {"status": "done", "summary": "ok", "key": None, "stage": "x", "waiting": None,
                  "artifacts": [], "checks": []}
        with patch("factory.runtime.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, json.dumps(result), "")) as run, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.worker(self.cfg, "builder", "Unit", str(brief)), 0)
        argv, env = run.call_args.args[0], run.call_args.kwargs["env"]
        self.assertIn("CLAUDE_CODE_DISABLE_AUTO_MEMORY=1", argv)
        self.assertIn("SHARED=worker", argv)
        self.assertNotIn("SHARED=launch", argv)
        self.assertEqual((env["SHARED"], env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"]), ("worker", "1"))

    def test_min_env_off_switch_drops_launch_env_and_judge_extras(self):
        (self.state / "min-env").write_text("off\n")
        call = self.wake_call()
        self.assertNotIn("CLAUDE_CODE_DISABLE_AUTO_MEMORY=1", call.args[0])
        self.assertNotIn("env", call.kwargs)
        self.data["judge"] = {"command": ["model"], "extra_args": ["--strict"]}
        runner = self.judge_runner()
        judge.ask(Config(self.data, self.config), {}, runner=runner)
        self.assertEqual(runner.argv, ["model"])
        self.assertIsNone(runner.env)

    def test_judge_gets_extra_args_and_launch_environment(self):
        self.data["judge"] = {"command": ["model", "--schema", "{schema}"], "extra_args": ["--tools", "Read"]}
        runner = self.judge_runner()
        self.assertEqual(judge.ask(Config(self.data, self.config), {}, runner=runner)["decision"], "escalate")
        self.assertEqual(runner.argv[-2:], ["--tools", "Read"])
        self.assertEqual(runner.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")

    def judge_runner(self):
        def runner(argv, **kwargs):
            runner.argv, runner.env = argv, kwargs.get("env")
            return subprocess.CompletedProcess(argv, 1, "{}", "")
        runner.argv = runner.env = None
        return runner

    def test_invalid_settings_are_rejected(self):
        for section, value, message in (("launch", {"env": {"A=B": "1"}}, "launch.env"),
                                        ("launch", {"env": {"A": 1}}, "launch.env"),
                                        ("judge", {"command": ["m"], "extra_args": "--x"}, "judge.extra_args"),
                                        ("dream", {"command_check": 3}, "dream")):
            with self.subTest(section=section), self.assertRaisesRegex(ValueError, message):
                Config({**self.data, section: value}, self.config)


class QueueNoteCapTests(Fixture):
    def note(self):
        return self.read("queue.json")["PROJ-123"]["note"]

    def test_short_note_is_unchanged(self):
        state_patch(self.state, "queue.json", ["PROJ-123"], {"note": "one | two"})
        self.assertEqual(self.note(), "one | two")
        self.assertFalse((self.state / "notes").exists())

    def test_long_note_moves_oldest_entries_and_keeps_newest_last(self):
        entries = [f"entry {n:03d} " + "x" * 90 for n in range(60)]
        state_patch(self.state, "queue.json", ["PROJ-123"], {"note": " | ".join(entries)})
        note = self.note()
        self.assertTrue(note.startswith(note_marker("PROJ-123")))
        self.assertLessEqual(len(note), NOTE_MAX_CHARS)
        self.assertTrue(note.endswith(entries[-1]))
        moved = (self.state / "notes/PROJ-123.md").read_text()
        self.assertIn(entries[0], moved)
        kept = note[len(note_marker("PROJ-123")):].split(" | ")
        self.assertTrue(set(kept) <= set(entries))  # No entry was cut in half.
        self.assertEqual(set(kept) | {e for e in entries if e in moved}, set(entries))
        state_patch(self.state, "queue.json", ["PROJ-123"], {"note": note + " | newest"})
        self.assertTrue(self.note().endswith(" | newest"))
        self.assertEqual(self.note().count(note_marker("PROJ-123")), 1)

    def test_prepended_entry_is_reordered_last_and_line_limit_applies(self):
        lines = "\n".join(f"line {n}" for n in range(NOTE_MAX_LINES + 5))
        state_patch(self.state, "queue.json", ["PROJ-123"], {"note": lines})
        old = self.note()
        self.assertLess(old.count("\n"), NOTE_MAX_LINES)
        self.assertIn("line 0", (self.state / "notes/PROJ-123.md").read_text())
        state_patch(self.state, "queue.json", ["PROJ-123"], {"note": "latest | " + old})
        self.assertTrue(self.note().endswith(" | latest"))


class RunsHygieneTests(Fixture):
    def setUp(self):
        super().setUp()
        self.data["liveness"] = {"enabled": True}
        self.data["launcher"]["list_cmd"] = [sys.executable, str(self.stub), "list"]
        self.cfg = Config(self.data, self.config)

    def test_non_run_folders_are_ignored_and_reported(self):
        token = "a" * 32
        for name in (token, "morning-evidence", "marked"):
            (self.state / "runs" / name).mkdir(parents=True)
        (self.state / "runs/marked/lost").touch()
        (self.state / "runs/dump.json").write_text("{}")
        self.assertEqual([path.name for path in runtime._pending_runs(self.cfg)], [token, "marked"])
        self.assertEqual(runtime.stray_runs(self.cfg), ["dump.json", "morning-evidence"])
        self.responses["list"] = {"stdout": "[]"}
        self.save_responses()
        self.assertEqual(runtime.blocked_keys(self.cfg, token="current"), set())
        listed = [call for call in self.calls() if call[0] == "list"]
        self.assertEqual(len(listed), 2)  # The evidence folder is never queried.
        report = self.cli("factory-guard", "--runs-report")
        self.assertEqual(json.loads(report.stdout), {"nonRun": ["dump.json", "morning-evidence"]})

    def test_current_lock_token_counts_even_when_not_hex(self):
        (self.state / "runs/current-run").mkdir(parents=True)
        self.write("running.json", {"token": "current-run", "started_at": time.time()})
        self.assertEqual([path.name for path in runtime._pending_runs(self.cfg)], ["current-run"])

    def test_big_files_in_old_archived_runs_are_gzipped_never_deleted(self):
        old, young, open_run = "b" * 32, "c" * 32, "d" * 32
        now = time.time()
        for name in (old, young, open_run):
            path = self.state / "runs" / name
            path.mkdir(parents=True)
            (path / "big.log").write_bytes(b"z" * 300)
            (path / "small.log").write_bytes(b"s")
            if name != open_run:
                (path / "ended").touch()
                (path / "archived").touch()
        os.utime(self.state / "runs" / old / "ended", (now - 8 * 86400,) * 2)
        with patch.object(runtime, "GZIP_BYTES", 100):
            self.assertEqual(runtime.compress_old_runs(self.cfg, now=now), (1, 0))
            self.assertEqual(runtime.compress_old_runs(self.cfg, now=now), (0, 0))
        run = self.state / "runs" / old
        self.assertFalse((run / "big.log").exists())
        self.assertEqual(gzip.decompress((run / "big.log.gz").read_bytes()), b"z" * 300)
        self.assertEqual((run / "small.log").read_bytes(), b"s")
        self.assertTrue((run / "retained").exists())
        for name in (young, open_run):
            self.assertTrue((self.state / "runs" / name / "big.log").exists())

    def test_gzip_is_limited_per_call(self):
        run = self.state / "runs" / ("e" * 32)
        run.mkdir(parents=True)
        for n in range(5):
            (run / f"big{n}.log").write_bytes(b"z" * 300)
        (run / "ended").touch()
        (run / "archived").touch()
        later = time.time() + 8 * 86400
        with patch.object(runtime, "GZIP_BYTES", 100):
            self.assertEqual(runtime.compress_old_runs(self.cfg, now=later), (3, 0))
            self.assertFalse((run / "retained").exists())
            self.assertEqual(runtime.compress_old_runs(self.cfg, now=later), (2, 0))
        self.assertTrue((run / "retained").exists())


class DreamTests(Fixture):
    def setUp(self):
        super().setUp()
        self.data["dream"] = {"command_check": [sys.executable, "-c", "import sys; sys.exit(10)"],
                              "command_brief": [sys.executable, "-c", "print('Consolidate memory.')"]}
        self.data["launch"] = {"env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}}
        self.data["launcher"]["env_args"] = ["--env", "{name}={value}"]
        self.cfg = Config(self.data, self.config)
        self.now = datetime(2026, 11, 16, 10).astimezone()

    def launches(self):
        return [call for call in self.calls() if call[0] == "launch"]

    def test_due_check_launches_one_dreamer_once_per_day(self):
        outcome, record = dream.tick(self.cfg, now=self.now)
        self.assertEqual((outcome, record["agentId"], record["day"]), ("launched", "run-123", "2026-11-16"))
        argv = self.launches()[0]
        self.assertEqual(argv[-1].strip(), "Consolidate memory.")
        self.assertIn("model-large", argv)  # The orchestrator role's model by default.
        self.assertIn("CLAUDE_CODE_DISABLE_AUTO_MEMORY=1", argv)
        self.assertEqual(self.read("dream-last.json")["outcome"], "launched")
        self.assertEqual(dream.tick(self.cfg, now=self.now + timedelta(hours=3)), ("already-today", None))
        self.assertEqual(len(self.launches()), 1)

    def test_unset_off_and_not_due_launch_nothing(self):
        self.assertEqual(dream.tick(Config({**self.data, "dream": {}}, self.config), now=self.now), ("unset", None))
        (self.state / "dream").write_text("off\n")
        self.assertEqual(dream.tick(self.cfg, now=self.now), ("off", None))
        (self.state / "dream").unlink()
        self.data["dream"]["command_check"] = [sys.executable, "-c", "pass"]
        self.assertEqual(dream.tick(Config(self.data, self.config), now=self.now)[0], "not-due")
        self.data["dream"]["command_check"] = [sys.executable, "-c", "import sys; sys.exit(3)"]
        outcome, record = dream.tick(Config(self.data, self.config), now=self.now + timedelta(days=1))
        self.assertEqual((outcome, record["reason"]), ("error", "check exit 3"))
        self.assertEqual(self.launches(), [])

    def test_running_previous_dreamer_skips(self):
        self.write("dream-last.json", {"day": "2026-11-15", "agentId": "dreamer-1"})
        self.data["launcher"]["inspect_cmd"] = [sys.executable, str(self.stub), "inspect", "{id}"]
        self.responses["inspect"] = {"stdout": json.dumps({"Status": "running"})}
        self.save_responses()
        outcome, record = dream.tick(Config(self.data, self.config), now=self.now)
        self.assertEqual((outcome, record["reason"]), ("skipped", "dreamer-running"))
        self.assertEqual(self.launches(), [])

    def test_failed_launch_is_recorded_not_raised(self):
        self.responses["launch"] = {"stdout": "", "code": 1}
        self.save_responses()
        outcome, record = dream.tick(self.cfg, now=self.now)
        self.assertEqual((outcome, record["reason"]), ("error", "launch: ValueError"))

    def test_idle_tick_runs_the_dream_and_manual_wake_does_not(self):
        (self.state / "done-today").write_text(date.today().isoformat())
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runtime.wake(self.cfg), 0)
        self.assertIn("idle:", output.getvalue())
        self.assertIn("factory-wake: dream launched", output.getvalue())
        self.assertEqual(len(self.launches()), 1)
