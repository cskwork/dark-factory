from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
import subprocess
from unittest.mock import patch

from factory import board_feed, flags, preflight, ratchet, runtime, state
from factory.config import Config, ModelPolicyError
from support import Fixture


class DeliveryLogTests(Fixture):
    def rows(self):
        return [json.loads(line) for line in (self.state / "delivery.jsonl").read_text().splitlines()]

    def test_patch_and_transaction_journal_only_queue_stage_changes(self):
        key = "PROJ-123"
        state.patch(self.state, "queue.json", [key], {"stage": "intake"})
        state.patch(self.state, "queue.json", [key], {"waiting": "reporter"})
        state.patch(self.state, "state.json", [key], {"stage": "unrelated"})
        state.transaction(self.state, "queue.json", lambda queue: queue[key].update(stage="build"))
        state.transaction(self.state, "queue.json", lambda queue: queue.pop(key))
        rows = self.rows()
        self.assertEqual([(r["key"], r["from"], r["to"]) for r in rows],
                         [(key, None, "intake"), (key, "intake", "build"), (key, "build", None)])
        for row in rows:
            self.assertEqual(set(row), {"key", "from", "to", "at"})
            self.assertIsNotNone(datetime.fromisoformat(row["at"]).tzinfo)

    def test_concurrent_queue_patches_produce_a_single_stage_chain(self):
        key = "PROJ-123"
        stages = [f"stage-{n}" for n in range(12)]

        def advance(stage):
            state.patch(self.state, "queue.json", [key], {"stage": stage})

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(advance, stages))
        rows = self.rows()
        self.assertEqual(rows[0]["from"], None)
        self.assertCountEqual([r["to"] for r in rows], stages)
        self.assertEqual([r["to"] for r in rows[:-1]], [r["from"] for r in rows[1:]])
        self.assertEqual(rows[-1]["to"], self.read("queue.json")[key]["stage"])

    def test_retry_repairs_partial_append_without_a_duplicate(self):
        key = "PROJ-123"
        state.patch(self.state, "queue.json", [key], {"stage": "intake"})
        with patch("factory.state._finish_delivery", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                state.patch(self.state, "queue.json", [key], {"stage": "build"})
        pending = self.read(".delivery-pending.json")["rows"][0]
        with (self.state / "delivery.jsonl").open("a") as log:
            log.write(json.dumps(pending)[:16])
        state.patch(self.state, "queue.json", [key], {"stage": "build"})
        self.assertEqual([row["to"] for row in self.rows()], ["intake", "build"])
        self.assertFalse((self.state / ".delivery-pending.json").exists())


class RatchetTests(Fixture):
    def setUp(self):
        super().setUp()
        self.data["ratchet"] = {}
        self.now = datetime(2026, 11, 23, 10, tzinfo=timezone.utc)

    def test_builtin_windows_ignore_judge_and_measure_missing_usage(self):
        decisions = [
            {"event": "decision", "by": "button", "at": self.now.isoformat()},
            {"event": "decision", "by": "judge", "at": self.now.isoformat()},
            {"event": "action", "by": "button", "at": self.now.isoformat()},
            {"event": "decision", "by": "button", "at": (self.now - timedelta(days=8)).isoformat()},
        ]
        lessons = [{"kind": "worker_fail", "observation": "Repeated problem", "at": self.now.isoformat()}] * 3
        lessons += [{"kind": "worker_fail", "what": "Other problem", "at": (self.now - timedelta(days=15)).isoformat()}] * 3
        for name, rows in (("decisions.jsonl", decisions), ("lessons.jsonl", lessons)):
            (self.state / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.write("runs/" + "a" * 32 + "/summary.json", {"startedAt": self.now.isoformat(), "units": []})
        self.write("runs/" + "b" * 32 + "/summary.json", {"startedAt": self.now.isoformat(), "units": [
            {"usage": None}, {"usage": {"tokens": 19, "costUsd": None}},
        ]})
        self.write("runs/" + "c" * 32 + "/summary.json",
                   {"startedAt": (self.now - timedelta(days=8)).isoformat(), "units": []})
        self.write("running.json", {"token": "d" * 32})
        self.write("runs/" + "d" * 32 + "/summary.json", {"startedAt": self.now.isoformat(), "units": []})
        result = ratchet.daily(self.cfg, self.now)
        self.assertEqual({name: row["value"] for name, row in result["metrics"].items()},
                         {"human_decisions_per_day": 1 / 7, "no_progress_run_ratio": 0.5,
                          "repeat_lesson_signatures": 1, "cost_unknown_ratio": 0.5})

    def test_daily_tightens_only_down_and_regressions_reach_lesson_and_board(self):
        self.data["ratchet"]["commands"] = {"open_incidents": ["metric"]}
        observed = 4

        def metric(command, **kwargs):
            self.assertEqual(command, ["metric"])
            self.assertLessEqual(kwargs["timeout"], ratchet.COMMAND_TIMEOUT)
            return subprocess.CompletedProcess([], 0, str(observed), "")

        with patch.object(self.cfg, "run", side_effect=metric) as runner:
            self.assertIsNotNone(ratchet.daily(self.cfg, self.now))
            self.assertIsNone(ratchet.daily(self.cfg, self.now + timedelta(hours=1)))
            observed = 2
            ratchet.daily(self.cfg, self.now + timedelta(days=1))
            observed = 3
            result = ratchet.daily(self.cfg, self.now + timedelta(days=2))
            self.assertEqual(runner.call_count, 3)
        data = self.read("ratchets.json")
        self.assertEqual(data["ceiling"]["open_incidents"], 2)
        self.assertEqual(data["baseline"]["open_incidents"], 4)
        self.assertEqual(result["metrics"]["open_incidents"]["status"], "regression")
        lesson = json.loads((self.state / "lessons.jsonl").read_text())
        self.assertEqual((lesson["kind"], lesson["metric"]), ("ratchet_regression", "open_incidents"))
        health = next(row for row in board_feed.snapshot(self.cfg, now=(self.now + timedelta(days=2)).isoformat())["health"]
                      if row["title"] == "Ratchets")
        self.assertIn({"label": "Open incidents", "level": "bad", "value": "3 / 2"}, health["rows"])

    def test_history_is_bounded_and_missing_ratios_are_unknown(self):
        for day in range(35):
            report = ratchet.daily(self.cfg, self.now + timedelta(days=day))
        stored = self.read("ratchets.json")
        self.assertEqual(len(stored["history"]), 30)
        self.assertEqual(stored["history"][-1], stored["last"])
        self.assertEqual(report["metrics"]["cost_unknown_ratio"],
                         {"value": None, "ceiling": None, "status": "unknown"})
        self.assertEqual(report["metrics"]["no_progress_run_ratio"]["status"], "unknown")

    def test_idle_and_finish_paths_share_the_daily_gate_and_default_is_off(self):
        del self.data["ratchet"]
        self.assertIsNone(ratchet.daily(self.cfg))
        self.assertFalse((self.state / "ratchets.json").exists())
        self.data["ratchet"] = {"commands": {"example": ["metric"]}}
        idle = ("factory-guard: skipping duplicate (idle: none)", None)
        with patch.object(self.cfg, "run", return_value=subprocess.CompletedProcess([], 0, "1", "")) as runner:
            with patch("factory.runtime.guard", return_value=idle), redirect_stdout(io.StringIO()):
                self.assertEqual(runtime.wake(self.cfg, dry_run=True), 0)
                self.assertFalse((self.state / "ratchets.json").exists())
                self.assertEqual(runtime.wake(self.cfg), 0)
            _, run = runtime.guard(self.cfg, manual=True)
            runtime.guard(self.cfg, release=True, token=run["token"])
        runner.assert_called_once()

    def test_failed_hook_keeps_ceiling_and_reports_error(self):
        self.data["ratchet"]["commands"] = {"example": ["metric"]}
        with patch.object(self.cfg, "run", side_effect=[
            subprocess.CompletedProcess([], 0, "4", ""), subprocess.TimeoutExpired("metric", 10),
        ]):
            ratchet.daily(self.cfg, self.now)
            report = ratchet.daily(self.cfg, self.now + timedelta(days=1))
        self.assertEqual(report["metrics"]["example"],
                         {"value": None, "ceiling": 4, "status": "error", "error": "command timeout"})

    def test_ratchet_wrapper_regression_exit_and_no_baseline_reset(self):
        result = self.cli("factory-ratchet", "init")
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.state / "decisions.jsonl").write_text(json.dumps({
            "event": "decision", "by": "button", "at": datetime.now(timezone.utc).isoformat(),
        }) + "\n")
        result = self.cli("factory-ratchet", "check", "--tighten", "--json")
        self.assertEqual(result.returncode, 10, result.stderr)
        metric = json.loads(result.stdout)["metrics"]["human_decisions_per_day"]
        self.assertEqual(metric, {"value": 1 / 7, "ceiling": 0, "status": "regression"})
        self.assertIn("regression", self.cli("factory-ratchet", "status").stdout)
        self.assertEqual(self.cli("factory-ratchet", "init").returncode, 2)


class FactoryFlagsAndModelsTests(Fixture):
    def test_model_policy_covers_top_level_and_named_worker_roles(self):
        self.data["models"] = {"forbidden": ["model-[lr]*"]}
        with self.assertRaisesRegex(ModelPolicyError, r"roles.orchestrator.model .* forbidden pattern"):
            Config(self.data, self.config)
        self.data["roles"]["orchestrator"]["model"] = "allowed"
        with self.assertRaisesRegex(ModelPolicyError, r"roles.workers.reviewer.model"):
            Config(self.data, self.config)
        self.data["roles"]["waker"]["model"] = "model-replaced"
        with self.assertRaisesRegex(ModelPolicyError, r"roles.waker.model"):
            Config(self.data, self.config)
        self.data["models"] = {"forbidden": []}
        Config(self.data, self.config)

    def test_preflight_reports_forbidden_role_before_running_probes(self):
        self.data["models"] = {"forbidden": ["model-small"]}
        self.save_config()
        with redirect_stdout(io.StringIO()) as output, patch("factory.config.subprocess.run") as runner:
            code = preflight.main(["--config", str(self.config)])
        self.assertEqual(code, 2)
        self.assertIn("roles.waker.model 'model-small' matches forbidden pattern", json.loads(output.getvalue())["error"])
        runner.assert_not_called()

    def test_state_switches_config_toggles_and_invalid_values_in_json(self):
        self.data.update(liveness={"enabled": True}, drain={"enabled": True}, ratchet={},
                         judge={"mode": "shadow", "command": ["judge-test"]},
                         dream={"command_check": ["check"], "command_brief": ["brief"]})
        for name, raw in (("min-env", "off comment\n"), ("judge", "on"), ("dream", "off"),
                          ("liveness", "invalid"), ("drain", "0")):
            (self.state / name).write_text(raw)
        self.data["triage"]["mode"] = "mistyped"
        with patch.dict(os.environ, {"FACTORY_JUDGE_MODE": "on"}):
            result = self.cli("factory-flags", "status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {row["name"]: row for row in json.loads(result.stdout)}
        self.assertEqual({name: rows[name]["current"] for name in ("min-env", "judge", "dream", "drain", "ratchet")},
                         {"min-env": "off", "judge": "on", "dream": "off", "drain": "0", "ratchet": True})
        self.assertTrue(rows["liveness"]["current"].startswith("invalid: "))
        self.assertTrue(rows["triage.mode"]["current"].startswith("invalid: "))
        self.assertEqual(rows["judge"]["default"], "shadow")
        self.assertEqual(rows["drain"]["path"], str(self.state / "drain"))
        for row in rows.values():
            self.assertEqual(set(row), {"name", "path", "default", "current", "kind", "purpose", "retireBy"})
            self.assertIn(row["kind"], ("kill-switch", "ramp", "mode", "setting"))
            self.assertIsNone(row["retireBy"])

    def test_flag_defaults_without_new_sections(self):
        rows = {row["name"]: row["current"] for row in flags.status(self.cfg)}
        self.assertEqual(rows["min-env"], "on")
        self.assertEqual(rows["judge"], "off")
        self.assertEqual(rows["liveness.enabled"], False)
        self.assertEqual(rows["decisions.surface"], "html")
        self.assertEqual(rows["ratchet"], False)
