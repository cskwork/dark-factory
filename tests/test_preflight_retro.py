"""Offline regressions for prerequisite ordering and fail-fast preflight checks."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from factory import preflight
from factory.config import Config


class PreflightRetroTests(unittest.TestCase):
    def config(self, probes=None, codes=None, errors=None):
        cfg = Config({"preflight": {"probes": probes if probes is not None else {
            "vpn": {"cmd": ["vpn"], "human_exit_codes": [3]},
            "db": {"cmd": ["db"], "requires": ["vpn"]},
            "network": {"cmd": ["network"], "requires": ["vpn"]},
            "local": {"cmd": ["local"]},
        }}}, Path(__file__).resolve().parents[1] / "factory.toml")
        self.calls = []

        def run(template, **values):
            name = template[0]
            self.calls.append(name)
            if name in (errors or {}):
                raise errors[name]
            return subprocess.CompletedProcess(template, (codes or {}).get(name, 0), "secret", "secret")

        cfg.run = run
        return cfg

    def test_failed_prerequisite_skips_network_and_db_commands(self):
        report, code = preflight.check(self.config(codes={"vpn": 3}))
        self.assertEqual((report["status"], code), ("HUMAN", 10))
        self.assertEqual(self.calls, ["vpn", "local"])
        self.assertEqual([row["status"] for row in report["results"]], ["HUMAN", "SKIP", "SKIP", "PASS"])
        for row in report["results"][1:3]:
            self.assertEqual((row["reason"], row["exit_code"]), ("dependency-failed", None))
            self.assertEqual(row["failed_requires"], ["vpn"])
        self.assertNotIn("secret", json.dumps(report))

    def test_requested_probe_includes_prerequisites_in_dependency_order(self):
        probes = {"db": {"cmd": ["db"], "requires": ["network"]},
                  "network": {"cmd": ["network"], "requires": ["vpn"]},
                  "vpn": {"cmd": ["vpn"]}, "local": {"cmd": ["local"]}}
        report, code = preflight.check(self.config(probes), ["db", "vpn", "db"])
        self.assertEqual((code, self.calls), (0, ["vpn", "network", "db"]))
        self.assertEqual([row["name"] for row in report["results"]], self.calls)

    def test_skipped_prerequisite_also_skips_transitive_dependents(self):
        probes = {"vpn": {"cmd": ["vpn"]}, "network": {"cmd": ["network"], "requires": ["vpn"]},
                  "db": {"cmd": ["db"], "requires": ["network"]}}
        report, code = preflight.check(self.config(probes, codes={"vpn": 1}))
        self.assertEqual((report["status"], code, self.calls), ("DOWN", 20, ["vpn"]))
        self.assertEqual(report["results"][-1]["failed_requires"], ["network"])

    def test_all_prerequisites_must_pass(self):
        probes = {"vpn": {"cmd": ["vpn"]}, "auth": {"cmd": ["auth"]},
                  "db": {"cmd": ["db"], "requires": ["vpn", "auth"]}}
        report, code = preflight.check(self.config(probes, codes={"auth": 1}), ["db"])
        self.assertEqual((code, self.calls), (20, ["vpn", "auth"]))
        self.assertEqual(report["results"][-1]["failed_requires"], ["auth"])

    def test_fail_fast_marks_every_later_probe_skipped(self):
        for failure, expected in [(1, 20), (3, 10)]:
            with self.subTest(failure=failure):
                report, code = preflight.check(self.config(codes={"vpn": failure}), fail_fast=True)
                self.assertEqual((code, self.calls), (expected, ["vpn"]))
                self.assertEqual([(row["status"], row["reason"], row["exit_code"])
                                  for row in report["results"][1:]], [("SKIP", "fail-fast", None)] * 3)

    def test_check_environment_accepts_fail_fast_and_success_runs_all(self):
        report, code = preflight.check_environment(self.config(), fail_fast=True)
        self.assertEqual((report["status"], code, self.calls), ("PASS", 0, ["vpn", "db", "network", "local"]))

    def test_command_errors_skip_dependents_and_keep_exit_code(self):
        for error, status, code in [(subprocess.TimeoutExpired("vpn", 1), "DOWN", 20),
                                    (FileNotFoundError(), "HUMAN", 10)]:
            with self.subTest(status=status):
                report, actual = preflight.check(self.config(errors={"vpn": error}))
                self.assertEqual((report["status"], actual, self.calls), (status, code, ["vpn", "local"]))

    def test_invalid_config_never_runs_any_commands(self):
        invalid = [
            {"cmd": ["db"], "requires": "vpn"},
            {"cmd": ["db"], "requires": ["missing"]},
            {"cmd": ["db"], "requires": [1]},
            {"cmd": ["db"], "requires": [""]},
            {"cmd": ["db"], "requires": ["db"]},
            {"cmd": []},
            {"cmd": ["db"], "human_exit_codes": [True]},
        ]
        for probe in invalid:
            with self.subTest(probe=probe):
                cfg = self.config({"vpn": {"cmd": ["vpn"]}, "db": probe})
                with self.assertRaises(ValueError):
                    preflight.check(cfg, ["vpn"])
                self.assertEqual(self.calls, [])

    def test_dependency_cycle_is_invalid_before_execution(self):
        cfg = self.config({"local": {"cmd": ["local"]},
                           "vpn": {"cmd": ["vpn"], "requires": ["db"]},
                           "db": {"cmd": ["db"], "requires": ["vpn"]}})
        with self.assertRaisesRegex(ValueError, "cyclic-preflight-requires"):
            preflight.check(cfg, ["local"])
        self.assertEqual(self.calls, [])

    def test_cli_fail_fast_and_config_error_codes(self):
        for invalid, expected in [(False, 10), (True, 2)]:
            with self.subTest(invalid=invalid):
                cfg = self.config(codes={"vpn": 3})
                if invalid:
                    cfg.data["preflight"]["probes"]["db"]["requires"] = ["missing"]
                output = io.StringIO()
                with patch.object(preflight, "load_config", return_value=cfg), contextlib.redirect_stdout(output):
                    code = preflight.main(["--fail-fast"])
                report = json.loads(output.getvalue())
                self.assertEqual(code, expected)
                self.assertEqual(self.calls, [] if invalid else ["vpn"])
                self.assertEqual(report["status"], "FAIL" if invalid else "HUMAN")


if __name__ == "__main__":
    unittest.main()
