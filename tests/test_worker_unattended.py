"""Unattended launches and attempt-scoped cancellation use offline runner doubles."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import re
import subprocess
import time
from unittest.mock import patch

from factory.runtime import wake, worker
from support import Fixture


class UnattendedWorkerTests(Fixture):
    def setUp(self):
        super().setUp()
        self.brief = self.root / "brief.md"
        self.brief.write_text("Complete the approved unit.")
        self.result = {"status": "done", "summary": "Verified", "key": "PROJ-123",
                       "stage": "built", "waiting": None, "artifacts": [], "checks": []}
        self.data["launcher"].update(
            command=["runner", "launch"],
            env_args=["--env", "{name}={value}"],
            attempt_label_args=["--label", "attempt={attempt}"],
            list_cmd=["runner", "list", "--attempt", "{attempt}"],
            cancel_cmd=["runner", "stop", "{id}"])
        self.operations = []
        self.launch_stdout = ""
        self.launch_code = 7
        self.initial_status = "running"
        self.final_status = "idle"
        self.cancel_code = 0
        self.list_count = 0
        self.checkpoint = False
        self.timeout = False
        self.bad_list = False
        self.disappear = False
        self.run_token = None

    def run_command(self, argv, **kwargs):
        self.operations.append((argv, kwargs))
        if argv[:2] == ["runner", "launch"]:
            self.attempt_id = next((arg.split("=", 1)[1] for arg in argv
                                    if arg.startswith("attempt=")), "missing")
            if self.checkpoint:
                path = re.search(r'checkpoint_path=(".*?") ', argv[-1]).group(1)
                with open(json.loads(path), "w") as stream:
                    json.dump(self.result, stream)
            if self.timeout:
                raise subprocess.TimeoutExpired(argv, 1, output=b"partial output")
            return subprocess.CompletedProcess(argv, self.launch_code, self.launch_stdout, "")
        if argv[:2] == ["runner", "list"]:
            self.list_count += 1
            rows = [] if self.disappear and self.list_count > 1 else [
                {"id": "worker-full-id", "status": self.initial_status if self.list_count == 1 else self.final_status,
                 "labels": {"attempt": self.attempt_id, "key": "PROJ-123", "run": self.run_token}},
                {"id": "unrelated", "status": "running", "labels": {"attempt": "other", "key": "PROJ-123"}}]
            return subprocess.CompletedProcess(argv, 0, "{}" if self.bad_list else json.dumps(rows), "")
        if argv[:2] == ["runner", "stop"]:
            return subprocess.CompletedProcess(argv, self.cancel_code, "", "stop rejected")
        self.fail("Unexpected command: " + repr(argv))

    def execute(self, **kwargs):
        with patch("factory.runtime.subprocess.run", side_effect=self.run_command), \
                redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = worker(self.cfg, "builder", "Unit", str(self.brief), key="PROJ-123", **kwargs)
        return code, output.getvalue(), errors.getvalue()

    def invocation(self):
        return json.loads(next(self.state.glob("**/invocation.json")).read_text())

    def test_environment_is_passed_to_agent_and_parent_context_is_removed(self):
        self.data["worker"] = {"env": {"BLAST_RADIUS_UNATTENDED": "1", "UNIT_VALUE": "spaces ; $literal"}}
        self.launch_code, self.launch_stdout = 0, json.dumps(self.result)
        with patch.dict(os.environ, {"PASEO_AGENT_ID": "parent", "PASEO_WORKSPACE_ID": "workspace"}):
            self.assertEqual(self.execute()[0], 0)
            self.assertEqual(os.environ["PASEO_AGENT_ID"], "parent")
        argv, options = self.operations[0]
        self.assertIn("BLAST_RADIUS_UNATTENDED=1", argv)
        self.assertIn("UNIT_VALUE=spaces ; $literal", argv)
        self.assertEqual(options["env"]["BLAST_RADIUS_UNATTENDED"], "1")
        self.assertNotIn("PASEO_AGENT_ID", options["env"])
        self.assertNotIn("PASEO_WORKSPACE_ID", options["env"])
        self.assertEqual(len(self.operations), 1)

    def test_environment_arguments_can_follow_the_launcher_interface(self):
        self.data["worker"] = {"env": {"UNATTENDED": "1"}}
        self.data["launcher"]["env_args"] = ["--variable={name}:{value}"]
        self.launch_code, self.launch_stdout = 0, json.dumps(self.result)
        self.assertEqual(self.execute()[0], 0)
        self.assertIn("--variable=UNATTENDED:1", self.operations[0][0])
        self.assertNotIn("--env", self.operations[0][0])

    def test_local_launcher_can_inherit_environment_without_cli_flags(self):
        self.data["worker"] = {"env": {"UNATTENDED": "1"}}
        del self.data["launcher"]["env_args"]
        self.launch_code, self.launch_stdout = 0, json.dumps(self.result)
        self.assertEqual(self.execute()[0], 0)
        self.assertEqual(self.operations[0][1]["env"]["UNATTENDED"], "1")
        self.assertNotIn("--env", self.operations[0][0])

    def test_default_environment_and_orchestrator_do_not_add_worker_settings(self):
        self.launch_code, self.launch_stdout = 0, json.dumps(self.result)
        self.assertEqual(self.execute()[0], 0)
        self.assertNotIn("--env", self.operations[0][0])
        self.data["worker"] = {"env": {"BLAST_RADIUS_UNATTENDED": "1"}}
        with patch("factory.runtime.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "owner", "")) as run, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(wake(self.cfg), 0)
        self.assertNotIn("--env", run.call_args.args[0])
        self.assertNotIn("env", run.call_args.kwargs)

    def test_failed_worker_cancels_only_its_attempt_and_confirms_stop(self):
        code, output, errors = self.execute()
        self.assertEqual(code, 7)
        stops = [argv for argv, _ in self.operations if argv[:2] == ["runner", "stop"]]
        self.assertEqual(stops, [["runner", "stop", "worker-full-id"]])
        self.assertEqual(self.list_count, 2)
        receipt = self.invocation()["cancellation"]
        self.assertEqual(receipt["cancelledAgents"], ["worker-full-id"])
        self.assertFalse(receipt["cancellationFailed"])
        self.assertIn("cancelled: worker-full-id", receipt["summary"])
        self.assertIn("cancelled: worker-full-id", errors)
        self.assertEqual(json.loads(output)["cancellation"], receipt)
        self.assertEqual(json.loads(next(self.state.glob("**/cancellation.json")).read_text()), receipt)

    def test_invalid_result_and_timeout_also_cancel(self):
        self.launch_code, self.launch_stdout = 0, "{}"
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.invocation()["cancellation"]["cancelledAgents"], ["worker-full-id"])
        self.assertEqual(self.invocation()["exitCode"], 2)
        self.timeout = True
        self.list_count = 0
        self.assertEqual(self.execute()[0], 124)
        self.assertEqual(self.operations[-2][0], ["runner", "stop", "worker-full-id"])

    def test_valid_checkpoint_prevents_cancellation_even_after_timeout(self):
        self.checkpoint = self.timeout = True
        code, output, _ = self.execute()
        self.assertEqual(code, 124)
        self.assertEqual(json.loads(output)["result"], self.result)
        self.assertEqual(len(self.operations), 1)
        self.assertNotIn("cancellation", self.invocation())

    def test_failed_cancel_and_unconfirmed_stop_are_explicit(self):
        for cancel_code, final_status, disappear in ((1, "idle", False), (0, "running", False), (0, "idle", True)):
            with self.subTest(cancel_code=cancel_code, status=final_status, disappear=disappear):
                self.cancel_code, self.final_status, self.disappear = cancel_code, final_status, disappear
                self.list_count = 0
                code, output, errors = self.execute()
                self.assertEqual(code, 7)
                receipt = json.loads(output)["cancellation"]
                self.assertTrue(receipt["cancellationFailed"])
                self.assertEqual(receipt["cancelledAgents"], [])
                self.assertIn("cancel failed: worker-full-id", errors)

    def test_missing_cancel_command_is_recorded_without_querying(self):
        del self.data["launcher"]["cancel_cmd"]
        code, output, errors = self.execute()
        self.assertEqual(code, 7)
        self.assertEqual(len(self.operations), 1)
        self.assertIn("cancel-unavailable", json.loads(output)["cancellation"]["summary"])
        self.assertEqual(errors, "")

    def test_missing_attempt_identity_is_explicit_instead_of_claiming_no_running_agent(self):
        del self.data["launcher"]["attempt_label_args"]
        code, output, errors = self.execute()
        self.assertEqual(code, 7)
        self.assertEqual(len(self.operations), 1)
        self.assertIn("cancel-unavailable", json.loads(output)["cancellation"]["summary"])
        self.assertIn("attempt", json.loads(output)["cancellation"]["summary"])

    def test_finished_agent_is_not_cancelled_and_bad_query_is_failure(self):
        self.initial_status = "idle"
        code, output, _ = self.execute()
        self.assertEqual(code, 7)
        self.assertFalse(json.loads(output)["cancellation"]["cancellationFailed"])
        self.assertEqual(len(self.operations), 2)
        self.bad_list = True
        code, output, errors = self.execute()
        self.assertEqual(code, 7)
        self.assertTrue(json.loads(output)["cancellation"]["cancellationFailed"])
        self.assertIn("cancel failed:", errors)

    def test_cancel_must_target_an_id(self):
        self.data["launcher"]["cancel_cmd"] = ["runner", "stop-all"]
        code, output, _ = self.execute()
        self.assertEqual(code, 7)
        self.assertTrue(json.loads(output)["cancellation"]["cancellationFailed"])
        self.assertEqual(len(self.operations), 1)

    def test_cancellation_honors_field_and_status_mappings(self):
        self.data["launcher"].update(
            list_field="payload.sessions",
            fields={"id": "identity.uuid", "status": "state.phase", "labels": "tags", "key": "ticket"},
            status_map={"active": "running", "stopped": "idle"})
        original = self.run_command

        def mapped(argv, **kwargs):
            response = original(argv, **kwargs)
            if argv[:2] == ["runner", "list"]:
                rows = [{"identity": {"uuid": row["id"]},
                         "state": {"phase": "active" if row["status"] == "running" else "stopped"},
                         "tags": [name + "=" + value for name, value in row["labels"].items() if value],
                         "ticket": "PROJ-123"} for row in json.loads(response.stdout)]
                response.stdout = json.dumps({"payload": {"sessions": rows}})
            return response

        with patch.object(self, "run_command", side_effect=mapped):
            code, output, _ = self.execute()
        self.assertEqual(code, 7)
        self.assertEqual(json.loads(output)["cancellation"]["cancelledAgents"], ["worker-full-id"])

    def test_cancel_preserves_run_lock_and_exact_run_scope(self):
        self.run_token = "owner"
        current = {"token": self.run_token, "started_at": time.time()}
        self.write("running.json", current)
        self.data["launcher"]["run_label_args"] = ["--label", "run={token}"]
        code, output, _ = self.execute(token=self.run_token)
        self.assertEqual(code, 7)
        self.assertEqual(self.read("running.json"), current)
        self.assertEqual(json.loads(output)["cancellation"]["cancelledAgents"], ["worker-full-id"])

    def test_attempt_labels_are_required_when_list_is_not_attempt_scoped(self):
        self.data["launcher"]["list_cmd"] = ["runner", "list"]
        code, output, _ = self.execute()
        self.assertEqual(code, 7)
        self.assertEqual(json.loads(output)["cancellation"]["cancelledAgents"], ["worker-full-id"])
        self.assertNotIn(["runner", "stop", "unrelated"], [argv for argv, _ in self.operations])
