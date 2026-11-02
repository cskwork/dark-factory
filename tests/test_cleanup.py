"""Orchestrator-side worktree cleanup against temporary local Git repositories only."""
from contextlib import redirect_stdout
import io
import json
import os
import subprocess
from unittest.mock import patch

from factory import cleanup
from support import Fixture

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "factory", "GIT_AUTHOR_EMAIL": "factory@example.invalid",
           "GIT_COMMITTER_NAME": "factory", "GIT_COMMITTER_EMAIL": "factory@example.invalid"}


class CleanupTests(Fixture):
    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, GIT_ENV)
        environment.start()
        self.addCleanup(environment.stop)
        origin, self.repo, self.trees = self.root / "origin.git", self.root / "repo", self.root / "worktrees"
        self.sh(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        self.sh(self.root, "init", "-q", "-b", "main", str(self.repo))
        self.sh(self.repo, "remote", "add", "origin", str(origin))
        self.sh(self.repo, "commit", "-q", "--allow-empty", "-m", "base")
        self.sh(self.repo, "push", "-q", "origin", "main", "main:dev")
        self.sh(self.repo, "fetch", "-q", "origin")
        self.data["env_branches"] = {"dev": "origin/dev", "prd": "origin/main"}
        self.data["cleanup"] = {"roots": ["worktrees"]}

    def sh(self, where, *args):
        subprocess.run(["git", "-C", str(where), *args], check=True, capture_output=True, text=True)

    def tree(self, name, branch, start="origin/main", push=None):
        path = self.trees / name
        self.sh(self.repo, "worktree", "add", "-q", "-b", branch, str(path), start)
        self.sh(path, "commit", "-q", "--allow-empty", "-m", name)
        if push:
            self.sh(path, "push", "-q", "origin", "HEAD:" + push)
        return path

    def run_cleanup(self, *args):
        self.save_config()
        out = io.StringIO()
        with redirect_stdout(out):
            code = cleanup.main(["--config", str(self.config), *args])
        return code, json.loads(out.getvalue())

    def test_removes_only_listed_pushed_worktrees_and_promoted_merge_branch(self):
        merged = self.tree("merged", "merge/PROJ-1-dev", "origin/dev", push="dev")
        unpushed = self.tree("unpushed", "merge/PROJ-1-local")
        other = self.tree("ticket-only", "merge/PROJ-1-main", push="fix/PROJ-1")
        dirty = self.tree("dirty", "fix/PROJ-1-dirty", push="fix/PROJ-1-dirty")
        (dirty / "scratch.txt").write_text("not committed")
        unlisted = self.tree("unlisted", "merge/PROJ-2-dev", "origin/dev", push="dev")
        self.sh(self.repo, "fetch", "-q", "origin")
        result = self.root / "result.json"
        result.write_text(json.dumps({"artifacts": [
            "verify.json", "cleanup:" + str(merged), "cleanup:" + str(unpushed), "cleanup:" + str(other),
            "cleanup:" + str(dirty), "cleanup:" + str(self.repo), "cleanup:worktrees/merged",
            "cleanup:" + str(self.trees / "missing")]}))
        reasons = {"unpushed": "unpushed", "dirty": "dirty", "repo": "outside-roots",
                   "merged": "not-absolute", "missing": "not-worktree"}

        code, out = self.run_cleanup("--result", str(result))  # dry run removes nothing
        self.assertEqual(code, 0)
        self.assertEqual(out["mode"], "dry-run")
        self.assertEqual(out["removable"], [str(merged), str(other)])
        self.assertEqual((out["removed"], out["branchesDeleted"]), ([], []))
        self.assertEqual({row["path"].rsplit("/", 1)[-1]: row["reason"] for row in out["kept"]}, reasons)
        self.assertTrue(merged.exists())

        code, out = self.run_cleanup("--execute", "--result", str(result))
        self.assertEqual(code, 0, out)
        self.assertEqual(out["removed"], [str(merged), str(other)])
        self.assertEqual(out["branchesDeleted"], ["merge/PROJ-1-dev"])
        self.assertEqual(out["failed"], [])
        self.assertFalse(merged.exists() or other.exists())
        self.assertTrue(unpushed.exists() and dirty.exists() and unlisted.exists())
        branches = subprocess.run(["git", "-C", str(self.repo), "branch", "--list", "merge/*"],
                                  capture_output=True, text=True).stdout
        self.assertNotIn("merge/PROJ-1-dev", branches)
        self.assertIn("merge/PROJ-1-main", branches)  # tip only on a ticket branch, not a target
        self.assertIn("merge/PROJ-2-dev", branches)    # never listed by the worker

    def test_without_roots_every_target_is_kept(self):
        merged = self.tree("merged", "merge/PROJ-1-dev", "origin/dev", push="dev")
        del self.data["cleanup"]
        result = self.root / "result.json"
        result.write_text(json.dumps({"artifacts": ["cleanup:" + str(merged)]}))
        code, out = self.run_cleanup("--execute", "--result", str(result))
        self.assertEqual(code, 0)
        self.assertEqual(out["kept"], [{"path": str(merged), "reason": "outside-roots"}])
        self.assertTrue(merged.exists())

    def test_invalid_result_or_configuration_is_rejected(self):
        result = self.root / "result.json"
        result.write_text(json.dumps({"artifacts": "cleanup:/tmp"}))
        self.save_config()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cleanup.main(["--config", str(self.config), "--result", str(result)]), 2)
        for settings in ({"roots": "worktrees"}, {"roots": [""]}, {"roots": ["w"], "merge_branch_prefix": ""}):
            with self.subTest(settings=settings):
                self.data["cleanup"] = settings
                self.save_config()
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cleanup.main(["--config", str(self.config), "--result", str(result)]), 2)
