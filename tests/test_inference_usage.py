from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
import subprocess
from unittest.mock import patch

from factory import board_feed, drain, judge, runtime, usage
from support import Fixture


VERDICT = {"decision": "decide", "option": "go", "risk": "low", "humanNeeded": "",
           "against": "Consider waiting", "why": "The evidence is complete", "confidence": 0.95}
PASEO_USAGE = {"InputTokens": 100, "OutputTokens": 20, "CachedTokens": 15, "CostUsd": 0.25}
EMPTY_USAGE = dict.fromkeys(PASEO_USAGE, 0)


def response(value, code=0):
    return subprocess.CompletedProcess([], code, json.dumps(value), "")


class InferenceUsageTests(Fixture):
    def setUp(self):
        super().setUp()
        self.data["launcher"] = {"command": ["paseo", "run", "--label", "role={role}"],
                                 "attempt_label_args": ["--label", "attempt={attempt}"],
                                 "run_label_args": ["--label", "run={token}"]}

    def register_orchestrator(self, token, started):
        target = usage.paseo_target(runtime.launcher(self.cfg, "orchestrator", token=token), token=token)
        usage.register_orchestrator(self.cfg, token, started, target, "codex")
        return (f"runs/{token}/{usage.ORCHESTRATOR_RECEIPT}",
                self.state / "pending-orchestrator-usage" / (token + ".json"))

    def test_reuses_unique_launch_labels_and_adds_only_necessary_label(self):
        worker = runtime.launcher(self.cfg, "builder", token="run-1", attempt="try-1")
        self.assertEqual(usage.paseo_target(worker, attempt="try-1")["selectors"], {"attempt": "try-1"})
        self.assertNotIn("factory-usage=try-1", worker)
        orchestrator = runtime.launcher(self.cfg, "orchestrator", token="run-1")
        self.assertEqual(usage.paseo_target(orchestrator, token="run-1")["selectors"],
                         {"run": "run-1", "role": "orchestrator"})
        self.assertNotIn("factory-usage=run-1", orchestrator)
        command = ["paseo", "run", "--label", "run=run-1", "--home=/tmp/test-paseo"]
        target = usage.paseo_target(command, token="run-1")
        self.assertEqual(command[-2:], ["--label", "factory-usage=run-1"])
        self.assertEqual(target["connection"], ["--home", "/tmp/test-paseo"])

    def test_worker_receipt_flows_into_summary_with_codex_cost_unknown(self):
        self.data["roles"]["workers"]["builder"]["provider"] = "codex"
        _, run = runtime.guard(self.cfg, manual=True)
        token = run["token"]
        brief = self.root / "brief.md"
        brief.write_text("Verify the change")
        result = {"key": "PROJ-123", "status": "done", "stage": "build", "waiting": None,
                  "summary": "Done", "artifacts": [], "checks": []}
        calls = []

        def probe(argv, **kwargs):
            calls.append(argv)
            self.assertLessEqual(kwargs["timeout"], usage.PROBE_TIMEOUT)
            if argv[1] == "ls":
                self.assertRegex(argv[-1], r"^attempt=[0-9a-f]{32}$")
                return response([{"Id": "worker-id", "Status": "idle"}])
            return response({"LastUsage": {**PASEO_USAGE, "CostUsd": 0}})

        with patch("factory.runtime.subprocess.run", return_value=response(result)), redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.worker(self.cfg, "builder", "Build", brief, token=token, key="PROJ-123",
                                            usage_runner=probe), 0)
        self.assertEqual(calls[-1], ["paseo", "agent", "inspect", "--json", "worker-id"])
        invocation, = (self.state / "runs" / token / "attempts").glob("*/invocation.json")
        receipt = json.loads(invocation.read_text())
        self.assertEqual(receipt["usage"]["tokens"], 120)
        self.assertEqual(receipt["usage"]["cachedTokens"], 15)
        self.assertIsNone(receipt["usage"]["costUsd"])
        self.assertIsNone(receipt["usageError"])
        drain.summary_init(self.cfg, token, 1, 1)
        self.save_config()
        with redirect_stdout(io.StringIO()) as output:
            code = drain.main(["--config", str(self.config), "--token", token, "summary-record",
                               "--invocation", str(invocation), "--runtime"])
        self.assertEqual(code, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["units"][0]["usage"], receipt["usage"])
        self.assertEqual(summary["metrics"]["tokens"]["value"], 120)
        self.assertEqual(summary["metrics"]["tokens"]["status"], "known")
        self.assertEqual(summary["metrics"]["costUsd"]["status"], "unknown")

        def unavailable(*args, **kwargs):
            raise FileNotFoundError("launcher unavailable")

        with patch("factory.runtime.subprocess.run", return_value=response(result)), redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.worker(self.cfg, "builder", "Build", brief, token=token, key="PROJ-123",
                                            usage_runner=unavailable), 0)
        attempts = (self.state / "runs" / token / "attempts").glob("*/invocation.json")
        failed_probe, = (json.loads(path.read_text()) for path in attempts if path != invocation)
        self.assertIsNone(failed_probe["usage"])
        self.assertEqual(failed_probe["usageError"], "paseo-usage-FileNotFoundError")

    def test_worker_retries_empty_last_usage_once_and_never_records_it_as_zero(self):
        _, run = runtime.guard(self.cfg, manual=True)
        token = run["token"]
        brief = self.root / "brief.md"
        brief.write_text("Verify the change")
        result = {"key": "PROJ-123", "status": "done", "stage": "build", "waiting": None,
                  "summary": "Done", "artifacts": [], "checks": []}
        attempts = self.state / "runs" / token / "attempts"
        agent = [{"id": "worker-id", "status": "idle"}]
        for retry_usage in (PASEO_USAGE, EMPTY_USAGE):
            with self.subTest(retry_usage=retry_usage):
                before = set(attempts.glob("*/invocation.json"))
                with patch("factory.runtime.subprocess.run", return_value=response(result)), \
                        patch("factory.runtime.time.sleep") as sleep, redirect_stdout(io.StringIO()), \
                        patch("factory.usage._probe", side_effect=[
                            agent, {"LastUsage": EMPTY_USAGE}, agent, {"LastUsage": retry_usage},
                        ]) as probe:
                    self.assertEqual(runtime.worker(self.cfg, "builder", "Build", brief, token=token, key="PROJ-123"), 0)
                sleep.assert_called_once_with(3)
                self.assertEqual(probe.call_count, 4)
                receipt, = set(attempts.glob("*/invocation.json")) - before
                observed = json.loads(receipt.read_text())
                if retry_usage == EMPTY_USAGE:
                    self.assertIsNone(observed["usage"])
                    self.assertEqual(observed["usageError"], "empty-last-usage")
                    self.assertEqual(observed["observations"], {})
                else:
                    self.assertEqual(observed["usage"]["costUsd"], 0.25)
                    self.assertIsNone(observed["usageError"])

    def test_usage_failures_are_explicit_and_non_paseo_does_not_query(self):
        target = usage.paseo_target(["paseo", "run"], attempt="abc")
        for observed, expected in ((response([]), "agent-not-found"),
                                   (response([{}, {}]), "ambiguous-agent-label"),
                                   (response([{"id": "x", "status": "running"}]), "agent-running"),
                                   (response({}, 1), "paseo-usage: ls exit 1")):
            with self.subTest(error=expected):
                with patch("factory.usage.subprocess.run", return_value=observed) as runner:
                    self.assertEqual(usage.paseo_usage(self.cfg, target, "codex"), (None, expected))
                    runner.assert_called_once()
        with patch("factory.usage.subprocess.run", side_effect=subprocess.TimeoutExpired("paseo", 3)) as runner:
            self.assertEqual(usage.paseo_usage(self.cfg, target, "codex"), (None, "paseo-usage-timeout"))
            self.assertEqual(usage.paseo_usage(self.cfg, None, "codex"), (None, "non-paseo-launcher"))
            runner.assert_called_once()
        with patch("factory.usage.subprocess.run", side_effect=[
            response([{"id": "x", "status": "idle"}]), response({"LastUsage": {}}),
        ]):
            self.assertEqual(usage.paseo_usage(self.cfg, target, "codex"), (None, "invalid-last-usage"))

    def test_later_wake_collects_detached_orchestrator_after_it_stops(self):
        with patch("factory.runtime.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "owner", "")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.wake(self.cfg, manual=True), 0)
        token = self.read("running.json")["token"]
        path = "runs/" + token + "/" + usage.ORCHESTRATOR_RECEIPT
        self.assertEqual(self.read(path)["usageError"], "awaiting-agent-end")
        runtime.guard(self.cfg, release=True, token=token)

        def probe(argv, **unused):
            if argv[1] == "ls":
                self.assertEqual(argv[-4:], ["--label", f"run={token}", "--label", "role=orchestrator"])
                return response([{"id": "owner", "status": state}])
            return response({"LastUsage": PASEO_USAGE})

        with patch("factory.runtime.guard", return_value=("factory-guard: skipping duplicate (idle: none)", None)), \
                patch("factory.usage.subprocess.run", side_effect=probe) as runner, redirect_stdout(io.StringIO()):
            state = "running"
            self.assertEqual(runtime.wake(self.cfg), 0)
            self.assertIsNone(self.read(path)["usage"])
            self.assertEqual(self.read(path)["usageError"], "agent-running")
            state = "idle"
            self.assertEqual(runtime.wake(self.cfg), 0)
            self.assertEqual(runtime.wake(self.cfg), 0)
            self.assertEqual(runner.call_count, 3)
        self.assertEqual(self.read(path)["usage"]["costUsd"], 0.25)
        self.assertEqual(list((self.state / "pending-orchestrator-usage").iterdir()), [])

    def test_orchestrator_retries_transient_probe_errors_on_later_wakes(self):
        started = 100_000
        receipt, pending = self.register_orchestrator("a" * 32, started)
        errors = [
            (subprocess.TimeoutExpired("paseo", 3), "paseo-usage-timeout"),
            (response({}, 5), "paseo-usage: ls exit 5"),
            (PermissionError("unavailable"), "paseo-usage-PermissionError"),
        ]
        for minute, (failure, error) in enumerate(errors, 1):
            with self.subTest(error=error), patch("factory.usage.subprocess.run", side_effect=[failure]) as runner:
                usage.collect_orchestrators(self.cfg, runner=runner, now=started + 60 * minute)
                runner.assert_called_once()
                self.assertTrue(pending.exists())
                self.assertEqual(self.read(receipt)["usageError"], error)
                self.assertIsNone(self.read(receipt)["usage"])
        with patch("factory.usage.subprocess.run", side_effect=[
            response([{"id": "owner", "status": "idle"}]), response({"LastUsage": PASEO_USAGE}),
        ]) as runner:
            usage.collect_orchestrators(self.cfg, runner=runner, now=started + 240)
        self.assertEqual(self.read(receipt)["usage"]["costUsd"], 0.25)
        self.assertFalse(pending.exists())

    def test_orchestrator_retry_expires_at_24_hours_with_last_error(self):
        started = 100_000
        deadline = started + 24 * 60 * 60
        receipt, pending = self.register_orchestrator("a" * 32, started)
        with patch("factory.usage.subprocess.run", side_effect=[
            response([{"id": "owner", "status": "idle"}]), response({}, 2),
        ]) as runner:
            usage.collect_orchestrators(self.cfg, runner=runner, now=deadline - 1)
        self.assertTrue(pending.exists())
        with patch("factory.usage.subprocess.run") as runner:
            usage.collect_orchestrators(self.cfg, runner=runner, now=deadline)
            runner.assert_not_called()
        self.assertFalse(pending.exists())
        self.assertEqual(self.read(receipt),
                         {"startedAt": started, "usage": None, "usageError": "paseo-usage: agent exit 2"})

    def test_detached_orchestrator_empty_last_usage_stays_pending(self):
        started = 100_000
        receipt, pending = self.register_orchestrator("a" * 32, started)
        agent = response([{"id": "owner", "status": "idle"}])
        with patch("factory.usage.subprocess.run", side_effect=[
            agent, response({"LastUsage": EMPTY_USAGE}), agent, response({"LastUsage": PASEO_USAGE}),
        ]) as runner:
            usage.collect_orchestrators(self.cfg, runner=runner, now=started + 1)
            self.assertTrue(pending.exists())
            self.assertEqual(runner.call_count, 2)
            self.assertEqual(self.read(receipt)["usageError"], "empty-last-usage")
            self.assertIsNone(self.read(receipt)["usage"])
            usage.collect_orchestrators(self.cfg, runner=runner, now=started + 30)
        self.assertEqual(self.read(receipt)["usage"]["costUsd"], 0.25)
        self.assertFalse(pending.exists())

    def test_corrupt_pending_orchestrators_are_quarantined_and_collection_continues(self):
        started = 100_000
        bad_json_receipt, bad_json = self.register_orchestrator("a" * 32, started)
        bad_data_receipt, bad_data = self.register_orchestrator("b" * 32, started)
        valid_receipt, valid = self.register_orchestrator("c" * 32, started)
        bad_json.write_text("{")
        bad_data.write_text(json.dumps({"startedAt": started, "target": {}}))
        for index, path in enumerate([bad_json, bad_data, valid]):
            os.utime(path, (started + index, started + index))
        with redirect_stderr(io.StringIO()) as errors, patch("factory.usage.subprocess.run", side_effect=[
            response([{"id": "owner", "status": "idle"}]), response({"LastUsage": PASEO_USAGE}),
        ]) as runner:
            usage.collect_orchestrators(self.cfg, runner=runner, now=started + 1)
            self.assertEqual(runner.call_count, 2)
        for path, receipt in ((bad_json, bad_json_receipt), (bad_data, bad_data_receipt)):
            self.assertFalse(path.exists())
            quarantine = path.with_suffix(".json.corrupt")
            self.assertTrue(quarantine.exists())
            self.assertIn(quarantine.name, errors.getvalue())
            self.assertEqual(self.read(receipt)["startedAt"], started)
            self.assertTrue(self.read(receipt)["usageError"].startswith("corrupt-pending-usage:"))
            self.assertIsNone(self.read(receipt)["usage"])
        self.assertEqual(self.read(valid_receipt)["usage"]["costUsd"], 0.25)
        self.assertFalse(valid.exists())

    def test_judge_logs_envelope_usage_and_null_for_non_model_rows(self):
        self.data["judge"] = {"mode": "shadow", "command": ["judge-test"]}
        spec = {"kind": "choice", "key": "PROJ-123", "decision_id": "D1", "options": [{"id": "go"}]}
        envelope = {"structured_output": VERDICT, "total_cost_usd": 0.42,
                    "usage": {"input_tokens": 14, "output_tokens": 5, "cache_read_input_tokens": 8,
                              "cache_creation_input_tokens": 3}}
        model = lambda data: judge.ask(self.cfg, data, runner=lambda *a, **k: response(envelope))
        with patch.dict(os.environ, {"FACTORY_JUDGE_MODE": "shadow"}):
            self.assertEqual(judge.choice(self.cfg, spec, model=model), judge.ESCALATE)
        judge.log(self.cfg, {"kind": "promotion", "verdict": "qa-done-status"})
        first, second = (json.loads(line) for line in (self.state / "judge.jsonl").read_text().splitlines())
        self.assertEqual(first["usage"]["tokens"], 30)
        self.assertEqual(first["usage"]["costUsd"], 0.42)
        self.assertIsNone(second["usage"])

    def test_board_preserves_unknowns_and_totals_today_separately(self):
        now = datetime(2026, 11, 23, 4, tzinfo=timezone.utc).timestamp()
        path = self.write("runs/" + "a" * 32 + "/summary.json", {"units": [
            {"startedAt": now - 30, "usage": {"tokens": 45, "costUsd": 0.125}},
            {"startedAt": now - 20, "usage": {"tokens": 15, "costUsd": None}},
        ]})
        os.utime(path, (now - 10, now - 10))
        orchestrator = self.write("runs/" + "a" * 32 + "/" + usage.ORCHESTRATOR_RECEIPT,
                                  {"startedAt": now - 60, "usage": {"tokens": 40, "costUsd": 0.25}})
        os.utime(orchestrator, (now - 10, now - 10))
        (self.state / "judge.jsonl").write_text(json.dumps({"at": now - 10, "usage": {"costUsd": 0.5}}) + "\n")
        snapshot = board_feed.snapshot(self.cfg, now=now)
        self.assertEqual([(row["tokens"], row["costUsd"]) for row in snapshot["runs"]], [(45, 0.125), (15, None)])
        health = next(row["rows"] for row in snapshot["health"] if row["title"] == "Today's inference")
        values = {row["label"]: row["value"] for row in health}
        self.assertEqual(values, {"Known cost": "$0.375000", "Unknown-cost units": "1",
                                  "Tokens": "100", "Judge cost": "$0.500000"})
