import contextlib
from datetime import timedelta
import fcntl
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import time
from unittest.mock import patch

from factory import board_feed, runtime
from support import Fixture


class BoardFeedTest(Fixture):
    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, {"SDLC_TODO_FEED": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.now = time.time() + 1
        self.out = self.root / "board" / "feed.json"

    def snapshot(self):
        return board_feed.snapshot(self.cfg, now=self.now)

    def test_stage_mapping_and_unknown_stage(self):
        stages = ["intent", "asis", "plan-approved", "build", "tobe", "dev-deployed", "stg", "audit",
                  "shipped", "custom-stage", "https://private.example"]
        queue = {f"PROJ-{i}": {"stage": stage} for i, stage in enumerate(stages)}
        self.write("queue.json", queue)
        rows = self.snapshot()["items"]
        self.assertEqual([(rows[key]["column"], rows[key]["chip"]) for key in queue], [
            ("intent", "Intent"), ("plan", "Plan"), ("plan", "Plan"), ("build", "Build"),
            ("evidence", "Evidence"), ("deliver", "dev"), ("deliver", "review"), ("deliver", "review"),
            ("done", "production"), (None, "custom-stage"), (None, None),
        ])

    def test_delivery_fields_and_legacy_stages_share_the_same_mapping(self):
        stages = {
            "dev-verified": ("deliver", "dev"),
            "result-posted": ("deliver", "dev"),
            "stg-ready": ("deliver", "review"),
            "stg-merged": ("deliver", "review"),
            "audit-verified": ("deliver", "review"),
            "audit-commented": ("deliver", "review"),
            "closed": ("done", "production"),
        }
        for stage, expected in stages.items():
            with self.subTest(stage=stage):
                self.assertEqual(board_feed._stage_display({"stage": stage}, []), expected)
                if stage != "closed":
                    self.assertEqual(board_feed._stage_display({"stage": "build", "delivery": stage}, []), expected)
        self.assertEqual(board_feed._stage_display({"stage": "plan", "delivery": "shipped"}, []),
                         ("done", "production"))
        self.assertEqual(board_feed._stage_display({"stage": "closed", "delivery": "none"}, []),
                         (None, "closed"))

    def test_native_and_compatible_worker_outcomes(self):
        self.assertEqual(board_feed._outcome({"worker": {"outcome": "completed"}}), "ok")
        self.assertEqual(board_feed._outcome({"worker": {"outcome": "failed"}}), "fail")
        self.assertEqual(board_feed._outcome({"worker": {"outcome": "timeout"}}), "unknown")
        self.assertEqual(board_feed._outcome({"invocation": {"outcome": "success", "exitCode": 0}}), "ok")
        self.assertEqual(board_feed._outcome({"invocation": {"outcome": "worker-timeout", "exitCode": 124}}), "fail")

    def test_explicit_waits_take_precedence_over_active_workers(self):
        waits = ["human-risk", "stg-approval", "reporter", "env-human", "env-down", None, "custom-wait", None]
        keys = [f"PROJ-{i}" for i in range(len(waits))]
        self.write("queue.json", {key: {"stage": "build", "waiting": waiting} for key, waiting in zip(keys, waits)})
        self.write("running.json", {"token": "current", "started_at": self.now - 10, "focusKey": keys[-1]})
        for i, key in enumerate(keys):
            self.write(f"runs/current/attempts/{i}/active.json", {
                "key": key, "pid": os.getpid(), "startedAt": self.now - 10,
            })
        self.write(f"runs/current/attempts/{len(keys) - 1}/invocation.json", {"endedAt": self.now - 1})
        self.write("runs/different/attempts/1/active.json", {
            "key": keys[-1], "pid": os.getpid(), "startedAt": self.now - 10,
        })
        payload = self.snapshot()
        self.assertEqual(payload["status"]["state"], "running")
        self.assertEqual(payload["status"]["lanes"], {"busy": 1, "total": 1})
        self.assertEqual([payload["items"][key]["turn"] for key in keys],
                         ["you", "you", "others", "blocked", "blocked", "agent", None, None])
        self.assertEqual(payload["items"][keys[6]]["waitingText"], "Waiting")

    def test_stale_ownership_waiting_and_custom_key_do_not_look_active(self):
        self.data["key_pattern"] = r"TASK\.\d+"
        self.write("queue.json", {"TASK.42": {"stage": "build", "waiting": "reporter"}})
        self.write("running.json", {"token": "current", "started_at": self.now - 10801})
        self.write("runs/current/attempts/owned/active.json", {
            "key": "TASK.42", "pid": os.getpid(), "startedAt": self.now - 10,
        })
        payload = self.snapshot()
        self.assertEqual(payload["status"]["state"], "idle")
        self.assertEqual(payload["items"]["TASK.42"]["turn"], "others")

    def test_missing_and_malformed_optional_inputs(self):
        defaults = self.snapshot()
        self.assertEqual(defaults["version"], 1)
        self.assertEqual(defaults["source"], "dark-factory")
        self.assertTrue(defaults["generatedAt"].endswith("+09:00"))
        self.assertEqual(defaults["status"]["state"], "idle")
        self.assertIsNone(defaults["status"]["nextRunAt"])
        self.assertEqual(defaults["health"][1]["rows"][0]["value"], "Unknown")
        for name in ("queue.json", "guard-last.json", "running.json", "scan-state.json",
                     "idle.json", "lesson-decisions/fixes.json", "runs/01010101010101010101010101010101/summary.json"):
            self.write(name, {"x": 1}).write_bytes(b"\xff")
        (self.state / "lessons.jsonl").write_text(
            json.dumps({"at": self.now - 10, "kind": "infra_fail"}) + "\n[bad]\n"
        )
        payload = self.snapshot()
        self.assertEqual(payload["items"], {})
        self.assertEqual(payload["runs"], [])
        self.assertEqual(payload["alerts"], [])
        self.assertEqual(payload["health"][2]["rows"][0]["value"], "1")

    def test_monitor_counts_and_free_text_stay_out_of_snapshot(self):
        private = "Example Person /private/workspace Bearer my-secret-token"
        self.write("scan-state.json", {
            "status": "degraded", "lastErrorAt": self.now - 30, "lastError": private,
            "sources": {
                "tracker": {"status": "available", "lastSuccessAt": self.now - 10, "lastError": private},
                "build-monitor": {"status": "degraded", "lastErrorAt": self.now - 30},
                private: {"status": "unavailable"},
            },
        })
        self.write("guard-last.json", {"at": "2026-11-18T23:42:05+00:00", "internalErrors": [private]})
        self.write("idle.json", {"shouldWake": False})
        self.write("queue.json", {"PROJ-1": {"stage": "tobe", "waiting": "human-risk", "action": private,
                                          "note": private, "runSheet": private, "service": "example-api",
                                          "slug": "work-item", "nextStep": "local", "createdAt": self.now - 100},
                                  "PROJ-2": {"stage": "build", "service": private, "nextStep": private,
                                             "slug": private, "updatedAt": private}})
        self.write("lesson-decisions/fixes.json", {"private-name": {"state": "open"}, "fixed": {"state": "fixed"}})
        (self.state / "lessons.jsonl").write_text(
            json.dumps({"at": self.now - 32 * 86400, "kind": "worker_fail", "what": private}) + "\n"
        )
        payload = self.snapshot()
        self.assertEqual(payload["status"]["state"], "idle")
        self.assertEqual({alert["level"] for alert in payload["alerts"]}, {"bad", "warn"})
        self.assertEqual(payload["alerts"][0], {
            "level": "warn", "text": "build-monitor monitoring degraded", "since": board_feed._iso(self.now - 30),
        })
        self.assertEqual(payload["alerts"][1]["since"], "2026-11-19T08:42:05+09:00")
        self.assertEqual(payload["health"][1]["rows"][0]["value"], "11-19 08:42")
        self.assertEqual(payload["health"][0]["rows"], [
            {"label": "Intake", "value": "Degraded", "level": "warn"},
            {"label": "build-monitor", "value": "Degraded", "level": "warn"},
            {"label": "tracker", "value": "Available · " + board_feed._iso(self.now - 10), "level": "ok"},
        ])
        self.assertEqual(payload["health"][2]["rows"][1]["value"], "1")
        self.assertIn({"label": "Worker failure", "value": "1", "level": None}, payload["health"][2]["rows"])
        self.assertEqual(payload["items"]["PROJ-1"]["nextStep"], "local")
        self.assertEqual(payload["items"]["PROJ-1"]["service"], "example-api")
        self.assertTrue(payload["items"]["PROJ-1"]["updatedAt"].endswith("+09:00"))
        self.assertIsNone(payload["items"]["PROJ-2"]["nextStep"])
        for hidden in ("Example Person", "/private/workspace", "my-secret-token", "private-name"):
            self.assertNotIn(hidden, json.dumps(payload))

    def test_recent_summaries_filter_mtime_and_unit_start(self):
        last_week = self.now - timedelta(days=7, seconds=1).total_seconds()
        before_midnight = board_feed._timestamp(self.now).replace(hour=0, minute=0, second=0, microsecond=0)
        today = (self.now + before_midnight.timestamp()) / 2
        common = {"key": "PROJ-1", "unit": "private /path", "endedAt": today, "stage": "built"}
        good = {**common, "startedAt": today, "invocation": {"outcome": "success", "exitCode": 0}}
        fail = {**common, "startedAt": today - 1, "invocation": {"outcome": "worker-launch-failed", "exitCode": 2}}
        unknown = {**common, "startedAt": today - 2}
        newest = self.write("runs/01010101010101010101010101010101/summary.json", {
            "units": [fail, good, unknown, {**common, "startedAt": last_week},
                      {**common, "startedAt": "invalid"}, None],
        })
        os.utime(newest, (today, today))
        old = self.write("runs/00000000000000000000000000000000/summary.json", {"units": [good]})
        os.utime(old, (last_week, last_week))
        other = self.write("runs/other/summary.json", {"units": [good]})
        with patch.object(board_feed, "_object_file", wraps=board_feed._object_file) as read:
            payload = self.snapshot()
        requested = [call.args[0] for call in read.call_args_list]
        self.assertNotIn(old, requested)
        self.assertNotIn(other, requested)
        self.assertEqual([run["outcome"] for run in payload["runs"]], ["unknown", "fail", "ok"])
        self.assertEqual([run["label"] for run in payload["runs"]], ["Build"] * 3)
        self.assertEqual(payload["health"][3]["rows"][0]["value"],
                         str(sum(run["startedAt"][:10] == payload["generatedAt"][:10] for run in payload["runs"])))

    def test_history_read_and_output_are_capped(self):
        root = self.state / "runs"
        folders = [root / str(i) for i in range(302)]

        def modified(path):
            return self.now - 1000 + int(path.parent.name)

        def contents(path):
            self.assertEqual(path.name, "summary.json")
            started = modified(path)
            return {"units": [
                {"key": "PROJ-1", "startedAt": started, "endedAt": started + 1},
                {"key": "PROJ-2", "startedAt": started - 0.5, "endedAt": started + 1},
            ]}

        with (patch.object(runtime, "run_folder", return_value=True),
              patch.object(board_feed, "_folders", return_value=folders),
              patch.object(board_feed, "_mtime", side_effect=modified),
              patch.object(board_feed, "_object_file", side_effect=contents) as read):
            runs, _ = board_feed._runs(self.cfg, board_feed._timestamp(self.now))
        self.assertEqual(sum(call.args[0].name == "summary.json" for call in read.call_args_list), 300)
        self.assertEqual(len(runs), 300)
        self.assertEqual(runs[0]["startedAt"], board_feed._iso(self.now - 848.5))
        self.assertEqual(runs[-1]["startedAt"], board_feed._iso(self.now - 699))

    def test_atomic_private_write_and_failed_replace_preserves_previous_data(self):
        self.out.parent.mkdir()
        self.out.write_text('{"old": true}')
        self.out.chmod(0o644)
        snapshot = board_feed.snapshot

        def locked_snapshot(*args, **kwargs):
            directory = os.open(self.out.parent, os.O_RDONLY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(directory)
            return snapshot(*args, **kwargs)

        with patch.object(board_feed, "snapshot", side_effect=locked_snapshot):
            board_feed.publish(self.cfg, self.out)
        previous = self.out.read_bytes()
        self.assertEqual(json.loads(previous)["version"], 1)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)
        with patch("factory.state.os.replace", side_effect=OSError("sensitive path")), self.assertRaises(OSError):
            board_feed.publish(self.cfg, self.out)
        self.assertEqual(self.out.read_bytes(), previous)
        self.assertEqual(list(self.out.parent.iterdir()), [self.out])

    def test_cli_precedence_and_default(self):
        self.data["board_feed"] = {"out": "configured/feed.json"}
        configured = self.root / "configured" / "feed.json"
        environment = self.root / "environment" / "feed.json"
        self.assertEqual(board_feed.output_path(self.cfg), configured)
        self.save_config()
        args = ["--config", str(self.config)]
        with patch.dict(os.environ, {"SDLC_TODO_FEED": str(environment)}):
            self.assertEqual(board_feed.main(args), 0)
            self.assertEqual(board_feed.main([*args, "--out", str(self.out)]), 0)
        self.assertTrue(environment.is_file())
        self.assertTrue(self.out.is_file())
        self.assertFalse(configured.exists())
        del self.data["board_feed"]
        self.save_config()
        with patch.object(board_feed, "publish") as writer:
            self.assertEqual(board_feed.main(args), 0)
        self.assertEqual(writer.call_args.args[1], Path("~/.sdlc/todo-feed.json").expanduser())

    def test_guard_outcomes_and_finalized_run_publish_outside_guard_lock(self):
        self.data["board_feed"] = {"out": str(self.out)}
        self.write("queue.json", {"PROJ-1": {"stage": "build"}})
        lock_path = self.state / ".guard.lock"
        publish = board_feed.publish

        def unlocked(*args, **kwargs):
            with lock_path.open("a") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return publish(*args, **kwargs)

        with patch.object(board_feed, "publish", side_effect=unlocked) as writer:
            _, run = runtime.guard(self.cfg, manual=True)
            first = json.loads(self.out.read_text())
            self.assertIsNone(first["items"]["PROJ-1"]["turn"])
            duplicate, next_run = runtime.guard(self.cfg)
            self.assertIn("duplicate (running)", duplicate)
            self.assertIsNone(next_run)
            runtime.guard(self.cfg, release=True, force=True)
            self.assertEqual(json.loads(self.out.read_text())["status"]["state"], "idle")
            self.write("last-wake.json", {"at": time.time()})
            duplicate, next_run = runtime.guard(self.cfg)
            self.assertIn("duplicate (window)", duplicate)
            self.assertIsNone(next_run)
            _, next_run = runtime.guard(self.cfg, manual=True)
            runtime.release_run(self.cfg, next_run, retry=True, unlaunched=True)
            self.assertEqual(json.loads(self.out.read_text())["status"]["state"], "idle")
            self.assertEqual(writer.call_count, 6)
        self.assertIsInstance(run, dict)

    def test_guard_preserves_return_and_error_when_feed_write_fails(self):
        with patch.object(board_feed, "publish") as writer:
            runtime.guard(self.cfg, manual=True)
        writer.assert_not_called()
        self.data["board_feed"] = {"out": str(self.out)}
        stderr = io.StringIO()
        with patch.object(board_feed, "publish", side_effect=OSError("/private/token")), contextlib.redirect_stderr(stderr):
            duplicate, run = runtime.guard(self.cfg)
            self.assertIn("duplicate (running)", duplicate)
            self.assertIsNone(run)
        self.assertEqual(stderr.getvalue(), "factory-board-feed: snapshot write failed\n")
        with patch.object(runtime, "_guard", side_effect=ValueError("native guard failure")):
            with self.assertRaisesRegex(ValueError, "native guard failure"):
                runtime.guard(self.cfg)
        self.assertIn("Guard check failed", self.out.read_text())

    def test_worker_active_marker_is_visible_only_during_owned_attempt(self):
        self.write("queue.json", {"PROJ-1": {"stage": "build"}})
        _, run = runtime.guard(self.cfg, manual=True)
        brief = self.root / "brief.md"
        brief.write_text("Implement the change.")
        result = json.dumps({"status": "done", "summary": "done", "key": "PROJ-1", "stage": "built",
                             "waiting": None, "artifacts": [], "checks": []})

        def during_attempt(command, **kwargs):
            payload = board_feed.snapshot(self.cfg)
            self.assertEqual(payload["items"]["PROJ-1"]["turn"], "agent")
            return subprocess.CompletedProcess(command, 0, stdout=result, stderr="")

        with patch("factory.runtime.subprocess.run", side_effect=during_attempt), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.worker(self.cfg, "builder", "Build", brief, token=run["token"], key="PROJ-1"), 0)
        self.assertIsNone(board_feed.snapshot(self.cfg)["items"]["PROJ-1"]["turn"])
        attempts = self.state / "runs" / run["token"] / "attempts"
        self.assertEqual(len(list(attempts.glob("*/invocation.json"))), 1)
        self.assertEqual(list(attempts.glob("*/active.json")), [])
