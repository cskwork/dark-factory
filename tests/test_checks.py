"""Offline branch, CI evidence and prerequisite checks using the standard library."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from factory import check_deploy, preflight
from factory.config import Config


ROOT = Path(__file__).resolve().parents[1]


class GitFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "Verifier", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "Verifier", "GIT_COMMITTER_EMAIL": "test@example.invalid",
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        self.git("init", "-b", "main")
        self.base = self.commit("baseline")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.git("checkout", "-b", "ticket")
        self.config_path = Path(self.temp.name) / "factory.toml"
        self.config_path.write_text('project_root = "."\nbase_branch = "origin/main"\n[ci.fields]\ncommit = "scm.commit"\n')

    def git(self, *args):
        response = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.repo), *args],
                                  capture_output=True, text=True, env=self.env, timeout=15)
        self.assertEqual(response.returncode, 0, response.stderr)
        return response.stdout.strip()

    def commit(self, subject):
        self.git("commit", "--allow-empty", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def cli(self, module, *args):
        response = subprocess.run([sys.executable, "-m", "factory." + module, "--config", str(self.config_path), *map(str, args)],
                                  cwd=ROOT, capture_output=True, text=True, env=self.env, timeout=15)
        self.assertEqual(response.stderr, "", response.stderr)
        self.assertEqual(len(response.stdout.splitlines()), 1, response.stdout)
        return response.returncode, json.loads(response.stdout)


class BaseChecks(GitFixture):
    def check(self, *args):
        return self.cli("check_base", "--repo", self.repo, "--branch", "ticket", "--key", "PROJ-123", *args)

    def test_clean_branch_and_historical_base_pass(self):
        self.commit("fix: selection [PROJ-123]")
        self.git("checkout", "main")
        advanced = self.commit("baseline advanced")
        self.git("update-ref", "refs/remotes/origin/main", advanced)
        code, report = self.check()
        self.assertEqual((code, report["status"], report["behind"]), (0, "PASS", 1))
        self.assertEqual(report["merge_base"], self.base)

    def test_foreign_commit_requires_explicit_key(self):
        self.commit("fix: selection [PROJ-123]")
        self.commit("fix: dependency [PROJ-124]")
        code, report = self.check()
        self.assertEqual((code, report["status"]), (1, "FAIL"))
        self.assertEqual(sum(not row["ok"] for row in report["commits"]), 1)
        self.assertEqual(self.check("--also-key", "PROJ-124")[0], 0)

    def test_marker_in_body_does_not_pass(self):
        self.commit("missing subject marker\n\n[PROJ-123]")
        self.assertEqual(self.check()[0], 1)

    def test_merge_commit_fails_even_when_tagged(self):
        self.commit("fix: first [PROJ-123]")
        self.git("checkout", "-b", "side", self.base)
        self.commit("fix: second [PROJ-123]")
        self.git("checkout", "ticket")
        self.git("merge", "--no-ff", "side", "-m", "merge [PROJ-123]")
        code, report = self.check()
        self.assertEqual((code, report["status"]), (1, "FAIL"))
        self.assertIn("merge-commit", report["commits"][0]["reason"])

    def test_unrelated_history_fails(self):
        self.git("checkout", "--orphan", "unrelated")
        self.commit("root [PROJ-123]")
        code, report = self.cli("check_base", "--repo", self.repo, "--branch", "unrelated", "--key", "PROJ-123")
        self.assertEqual((code, report["reason"]), (1, "no-common-base"))

    def test_invalid_ref_and_key_are_errors(self):
        self.assertEqual(self.check("--base", "missing")[0], 2)
        self.assertEqual(self.cli("check_base", "--repo", self.repo, "--branch", "ticket", "--key", "bad")[0], 2)


class DeployChecks(GitFixture):
    def setUp(self):
        super().setUp()
        self.expect = self.commit("fix: selection [PROJ-123]")
        self.checkout = self.commit("fix: follow-up [PROJ-123]")

    def check(self, *, status="completed", result="SUCCESS", checkout=None, number=7):
        fixture = Path(self.temp.name) / "run.json"
        fixture.write_text(json.dumps({"number": number, "status": status, "result": result,
                                       "scm": {"commit": self.checkout if checkout is None else checkout}}))
        return self.cli("check_deploy", "--repo", self.repo, "--job", "web-dev", "--build", "7",
                        "--expect", self.expect, "--run-json", fixture)

    def test_success_contains_expected_passes(self):
        code, report = self.check()
        self.assertEqual((code, report["status"], report["contains_expect"]), (0, "PASS", True))
        self.assertEqual(report["checkout_commit"], self.checkout)

    def test_running_failed_older_or_unknown_checkout_fail(self):
        cases = [(dict(status="running", result=None, checkout=""), "not-completed"),
                 (dict(result="FAILURE"), "result-not-success"),
                 (dict(checkout=self.base), "expect-not-contained"),
                 (dict(checkout="f" * 40), "checkout-commit-not-in-repo"),
                 (dict(checkout="HEAD"), "checkout-commit-invalid"),
                 (dict(checkout=""), "checkout-commit-missing")]
        for kwargs, reason in cases:
            with self.subTest(reason=reason):
                code, report = self.check(**kwargs)
                self.assertEqual((code, report["reason"], report["contains_expect"]), (1, reason, False))

    def test_build_mismatch_and_bad_json_are_errors(self):
        self.assertEqual(self.check(number=8)[0], 2)
        fixture = Path(self.temp.name) / "invalid.json"
        fixture.write_text("[]")
        code, _ = self.cli("check_deploy", "--repo", self.repo, "--job", "web-dev", "--build", "7",
                           "--expect", self.expect, "--run-json", fixture)
        self.assertEqual(code, 2)

    def test_configured_ci_command_and_nested_fields(self):
        cfg = Config({"ci": {"view_cmd": ["ci-tool", "--context", "{context}", "view", "{job}", "{build}"],
                              "fields": {"status": "run.status", "result": "run.result", "commit": "scm.checkout.sha"}}}, self.config_path)
        calls = []

        def run(template, **values):
            calls.append(cfg.command(template, **values))
            return subprocess.CompletedProcess(calls[-1], 0, json.dumps({"run": {"status": "completed", "result": "SUCCESS"},
                                                                         "scm": {"checkout": {"sha": self.checkout}}}))

        cfg.run = run
        output = io.StringIO()
        with patch.object(check_deploy, "load_config", return_value=cfg), contextlib.redirect_stdout(output):
            code = check_deploy.main(["--job", "job with spaces", "--build", "7", "--repo", str(self.repo), "--expect", self.expect, "--context", "stg"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [["ci-tool", "--context", "stg", "view", "job with spaces", "7"]])
        self.assertEqual(json.loads(output.getvalue())["status"], "PASS")


class PreflightChecks(unittest.TestCase):
    def config(self, codes=None, errors=None):
        cfg = Config({"preflight": {"probes": {"service": {"cmd": ["service-check"]},
                                              "auth": {"cmd": ["auth-check"], "human_exit_codes": [3], "human_action": "Sign in"}}}}, ROOT / "factory.toml")
        self.calls = []

        def run(template, **values):
            name = template[0].split("-")[0]
            self.calls.append(name)
            if errors and name in errors:
                raise errors[name]
            return subprocess.CompletedProcess(template, (codes or {}).get(name, 0), "secret-token", "secret-token")

        cfg.run = run
        return cfg

    def test_pass_and_requested_order(self):
        report, code = preflight.check(self.config(), ["auth", "service", "auth"])
        self.assertEqual((code, report["status"], self.calls), (0, "PASS", ["auth", "service"]))
        self.assertNotIn("secret-token", json.dumps(report))

    def test_human_takes_precedence_over_down(self):
        report, code = preflight.check(self.config({"service": 1, "auth": 3}))
        self.assertEqual((code, report["status"]), (10, "HUMAN"))
        self.assertEqual(report["results"][1]["human_action"], "Sign in")

    def test_timeout_is_down_and_missing_command_human(self):
        for error, expected in [(subprocess.TimeoutExpired("service-check", 1), 20), (FileNotFoundError(), 10)]:
            with self.subTest(expected=expected):
                report, code = preflight.check(self.config(errors={"service": error}), ["service"])
                self.assertEqual(code, expected)
                self.assertNotEqual(report["status"], "PASS")

    def test_bad_probe_never_starts_commands(self):
        cfg = self.config()
        with self.assertRaises(ValueError):
            preflight.check(cfg, ["service", "unknown"])
        self.assertEqual(self.calls, [])
        cfg.data["preflight"]["probes"]["auth"]["human_exit_codes"] = [0]
        with self.assertRaises(ValueError):
            preflight.check(cfg)
        self.assertEqual(self.calls, [])


class CliChecks(unittest.TestCase):
    def test_help_and_invalid_arguments(self):
        for module in ("check_base", "check_deploy", "preflight", "ticket_refs"):
            with self.subTest(module=module):
                response = subprocess.run([sys.executable, "-m", "factory." + module, "--help"], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(response.returncode, 0, response.stderr)
                self.assertIn("usage:", response.stdout)
                response = subprocess.run([sys.executable, "-m", "factory." + module, "--unknown"], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(response.returncode, 2, response.stderr)
                self.assertEqual(len(response.stdout.splitlines()), 1)
                json.loads(response.stdout)


if __name__ == "__main__":
    unittest.main()
