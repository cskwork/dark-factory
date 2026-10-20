"""Offline discovery against a temporary bare origin and local working tree."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from factory import ticket_refs
from factory.config import Config


ROOT = Path(__file__).resolve().parents[1]


class TicketRefs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.origin, self.repo = root / "origin.git", root / "service"
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "Verifier", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "Verifier", "GIT_COMMITTER_EMAIL": "test@example.invalid",
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True, capture_output=True, env=self.env)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True, capture_output=True, env=self.env)
        self.git("remote", "add", "origin", str(self.origin))
        self.commit("baseline")
        self.git("push", "-q", "origin", "main")
        for branch in ("dev", "stg"):
            self.git("push", "-q", "origin", "main:" + branch)
        self.config_path = root / "factory.toml"
        self.config_path.write_text('project_root = "."\nrepos = ["service"]\n')
        self.cfg = Config({"project_root": ".", "repos": ["service"]}, self.config_path)

    def git(self, *args):
        response = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.repo), *args],
                                  capture_output=True, text=True, env=self.env, timeout=15)
        self.assertEqual(response.returncode, 0, response.stderr)
        return response.stdout.strip()

    def commit(self, subject):
        self.git("commit", "--allow-empty", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def branch(self, name, subjects, start="main"):
        self.git("checkout", "-q", "-b", name, start)
        for subject in subjects:
            self.commit(subject)
        self.git("push", "-q", "origin", name)
        self.git("checkout", "-q", "main")

    def scan(self, key):
        return ticket_refs.scan_repo(self.repo, key, self.cfg)

    def cli(self, key):
        response = subprocess.run([sys.executable, "-m", "factory.ticket_refs", "--config", str(self.config_path), key],
                                  cwd=ROOT, capture_output=True, text=True, env=self.env, timeout=15)
        self.assertEqual(response.stderr, "", response.stderr)
        return response.returncode, json.loads(response.stdout)

    def test_own_branch_and_environment_containment(self):
        self.branch("fix/PROJ-123", ["fix: selection [PROJ-123]"])
        row = self.scan("PROJ-123")
        self.assertEqual(row["branches"], [{"branch": "fix/PROJ-123", "own": True, "other_keys": []}])
        self.assertEqual(row["environments"], {"dev": False, "stg": False, "prd": False})
        self.git("push", "-q", "origin", "fix/PROJ-123:dev")
        self.assertTrue(self.scan("PROJ-123")["environments"]["dev"])
        self.assertEqual(self.cli("PROJ-123")[1]["status"], "own-branch")

    def test_carried_branch_lists_other_promoted_keys(self):
        self.branch("feat/PROJ-100", ["feat: parent [PROJ-100]", "fix: follow-up [PROJ-123]"])
        self.git("push", "-q", "origin", "feat/PROJ-100:dev")
        row = self.scan("PROJ-123")
        self.assertEqual(row["branches"], [{"branch": "feat/PROJ-100", "own": False, "other_keys": ["PROJ-100"]}])
        self.assertTrue(row["environments"]["dev"])
        code, report = self.cli("PROJ-123")
        self.assertEqual((code, report["status"]), (0, "carried"))

    def test_none_uses_subject_only_and_exact_marker(self):
        self.branch("fix/PROJ-100", ["missing subject marker\n\n[PROJ-123]", "fix: longer [PROJ-1234]"])
        self.assertIsNone(self.scan("PROJ-123"))
        self.assertEqual(self.cli("PROJ-123"), (1, {"key": "PROJ-123", "status": "none", "repos": []}))

    def test_base_commits_are_excluded(self):
        self.branch("fix/PROJ-123", ["fix: released [PROJ-123]"])
        self.git("push", "-q", "origin", "fix/PROJ-123:main")
        self.assertIsNone(self.scan("PROJ-123"))

    def test_missing_base_or_environment_ref_is_explicit_error(self):
        self.branch("fix/PROJ-123", ["fix: selection [PROJ-123]"])
        for ref in ("refs/remotes/origin/stg", "refs/remotes/origin/main"):
            with self.subTest(ref=ref):
                sha = self.git("rev-parse", ref)
                self.git("update-ref", "-d", ref)
                code, report = self.cli("PROJ-123")
                self.assertEqual((code, report["status"]), (2, "FAIL"))
                self.assertIn("error", report)
                self.git("update-ref", ref, sha)

    def test_disconnected_remote_never_fetches(self):
        self.branch("fix/PROJ-123", ["fix: selection [PROJ-123]"])
        self.git("remote", "set-url", "origin", str(Path(self.tmp.name) / "missing-origin"))
        self.assertEqual(self.cli("PROJ-123")[0], 0)

    def test_custom_marker_branch_prefix_and_repository_glob(self):
        self.cfg.data.update(key_marker="ticket:{key}", key_pattern=r"^[A-Z]+-[0-9]+$", branch_prefixes=["work/"], repos="serv*")
        self.branch("work/PROJ-123", ["fix: selection ticket:PROJ-123", "fix: helper ticket:PROJ-124"])
        row = self.scan("PROJ-123")
        self.assertTrue(row["branches"][0]["own"])
        self.assertEqual(row["branches"][0]["other_keys"], ["PROJ-124"])
        self.assertEqual(ticket_refs.discover_repos(self.cfg), [self.repo.resolve()])


if __name__ == "__main__":
    unittest.main()
