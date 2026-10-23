"""Cross-module checks use local commands and private temporary state only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from factory.config import Config, load_config
from factory.state import locked
from support import Fixture, ROOT


class PortableIntegrationTests(Fixture):
    def test_example_opts_into_features_without_changing_legacy_defaults(self):
        example = load_config(ROOT / "factory.example.toml")
        self.assertTrue(example.get("liveness")["enabled"])
        self.assertTrue(example.get("drain")["enabled"])
        self.assertIsNone(self.cfg.get("liveness"))
        self.assertIsNone(self.cfg.get("drain"))
        settings = example.get("launcher")
        for name in ("list_cmd", "inspect_cmd", "archive_cmd", "logs_cmd"):
            argv = example.command(settings[name], token="token", id="agent-id", key="PROJ-123")
            self.assertTrue(argv)
        self.assertEqual(settings["fields"]["inspect_archived"], "Archived")

    def test_feature_switches_reject_ambiguous_values(self):
        for feature in ("liveness", "drain"):
            for invalid in ("off", 0, [], None, {"enabled": "false"}):
                with self.subTest(feature=feature, invalid=invalid), self.assertRaises(ValueError):
                    Config({feature: invalid}, self.config)
            for enabled in (True, False):
                self.assertEqual(Config({feature: {"enabled": enabled}}, self.config).get(feature)["enabled"], enabled)

    def test_approval_writer_uses_same_lock_as_state_patches(self):
        """A decision cannot read a stale ledger while another state writer holds it."""
        from factory.state import patch
        self.write("approvals.json", {"PROJ-2": {"approved": True, "note": "keep"}})
        data = {**self.data, "decisions": {"approval": {
            "on_approve": [sys.executable, str(self.stub), "approved"]}}}
        script = '''import json, pathlib, sys
from factory.config import Config
from factory.decision_page import _approve
cfg = Config(json.loads(sys.argv[1]), pathlib.Path(sys.argv[2]))
print(json.dumps(_approve(cfg, {"key":"PROJ-1", "decision_id":"decision-one"}, "reviewed")))
'''
        with locked(self.state / ".state.lock"):
            process = subprocess.Popen([sys.executable, "-c", script, json.dumps(data), str(self.config)],
                                       cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            try:
                time.sleep(0.15)
                self.assertIsNone(process.poll(), "approval bypassed the shared state lock")
                self.assertFalse(self.calls(), "approval action started before acquiring the state lock")
            except BaseException:
                process.kill()
                process.communicate()
                raise
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, out + err)
        patch(self.state, "approvals.json", ["PROJ-2"], {"used_at": "consumed"})
        approvals = self.read("approvals.json")
        self.assertTrue(approvals["PROJ-1"]["approved"])
        self.assertEqual(approvals["PROJ-1"]["action_status"], "done")
        self.assertEqual(approvals["PROJ-2"], {"approved": True, "note": "keep", "used_at": "consumed"})

    def test_entrypoints_keep_help_available_for_portable_commands(self):
        for binary in ("factory-guard", "factory-wake", "factory-worker"):
            result = self.cli(binary, "--help")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--config", result.stdout)
