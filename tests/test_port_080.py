"""Offline regressions for portable diagnostics, configuration and list probes."""
import json
import subprocess
from unittest.mock import patch

from factory import drain, idle, preflight
from factory.config import Config
from support import Fixture


class PortableRetroTests(Fixture):
    def test_capacity_is_rate_limited_in_result_or_stderr(self):
        output = self.root / "agent.txt"
        output.write_text("Selected model is at capacity")
        self.assertTrue(drain.rate_limited(output))
        output.write_text("SELECTED MODEL IS AT CAPACITY")
        self.assertTrue(drain.rate_limited(None, output))
        output.write_text("Model selection completed")
        self.assertFalse(drain.rate_limited(output))

    def test_internal_error_retains_type_and_300_message_characters(self):
        message = "broken " + "x" * 400
        with patch.object(idle, "pending_focus", side_effect=ValueError(message)):
            result, code = idle.decide(self.cfg)
        self.assertEqual(code, 2)
        self.assertIn("internal-error", result["reasons"])
        self.assertEqual(result["internalErrors"], ["ValueError: " + message[:300]])
        self.assertEqual(self.read("guard-last.json")["internalErrors"], result["internalErrors"])

    def test_guard_write_failure_is_returned_as_internal_error(self):
        with patch.object(idle, "write_json", side_effect=OSError("state unavailable")):
            result, code = idle.decide(self.cfg)
        self.assertEqual(code, 2)
        self.assertIn("OSError: state unavailable", result["internalErrors"])

    def test_lessons_thresholds_are_positive_integers(self):
        for name in ("escalate_count", "window_days", "cooldown_days"):
            for value in (0, -1, True, 1.5, "3"):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, "lessons"):
                    Config({"lessons": {name: value}}, self.config)
        for value in (None, [], "invalid"):
            with self.assertRaisesRegex(ValueError, "lessons"):
                Config({"lessons": value}, self.config)
        Config({"lessons": {"escalate_count": 2, "window_days": 1, "cooldown_days": 1}}, self.config)

    def test_worker_env_is_an_environment_string_map(self):
        for value in (None, [], "invalid", {"X": 1}, {"": "1"}, {"X=Y": "1"}, {"X": "a\0b"}, {"X\0Y": "1"}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "worker"):
                Config({"worker": {"env": value}}, self.config)
        with self.assertRaisesRegex(ValueError, "worker"):
            Config({"worker": []}, self.config)
        Config({"worker": {"env": {"BLAST_RADIUS_UNATTENDED": "1", "EMPTY": ""}}}, self.config)

    def probe(self, stdout, code=0, **probe):
        self.data["preflight"] = {"probes": {"resources": {"cmd": ["list"], **probe}}}
        with patch.object(self.cfg, "run", return_value=subprocess.CompletedProcess(["list"], code, stdout, "")):
            return preflight.check(self.cfg)

    def test_list_probe_requires_nonempty_matching_lines(self):
        for output in ("", " \n\t", "resource/one\nerror text", "resource/one extra"):
            with self.subTest(output=output):
                report, code = self.probe(output, stdout_pattern=r"resource/[a-z]+")
                self.assertEqual(code, 20)
                self.assertEqual(report["results"][0]["reason"], "invalid-command-list")
        report, code = self.probe("resource/one\n\nresource/two\n", stdout_pattern=r"resource/[a-z]+")
        self.assertEqual((code, report["status"]), (0, "PASS"))
        self.assertNotIn("resource/one", json.dumps(report))

    def test_list_probe_preserves_exit_status_and_existing_probes(self):
        report, code = self.probe("", 10, stdout_pattern=r"resource/.+", human_exit_codes=[10])
        self.assertEqual((code, report["status"]), (10, "HUMAN"))
        report, code = self.probe("")
        self.assertEqual((code, report["status"]), (0, "PASS"))

    def test_list_probe_validates_pattern_before_any_command(self):
        for pattern in ("[", "", 42, None):
            self.data["preflight"] = {"probes": {"list": {"cmd": ["list"], "stdout_pattern": pattern}}}
            with patch.object(self.cfg, "run") as run, self.subTest(pattern=pattern):
                with self.assertRaisesRegex(ValueError, "stdout-pattern"):
                    preflight.check(self.cfg)
                run.assert_not_called()
