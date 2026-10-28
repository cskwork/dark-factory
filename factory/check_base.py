"""Verify a ticket branch using local Git refs only. No fetch or writes occur."""

import argparse
import json
from pathlib import Path
import re
import subprocess

from .config import StateDirectoryError, load_config


TIMEOUT_SECONDS = 30


class CheckError(ValueError):
    """A fixed, safe reason for a failed check or invalid input."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CheckError("invalid-arguments; see --help")


def git_result(repo, stage, *args):
    try:
        return subprocess.run(
            ["git", "--no-pager", "-C", str(repo), *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", timeout=TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        raise CheckError("git-" + stage + "-timeout") from None
    except OSError:
        raise CheckError("git-" + stage + "-unavailable") from None


def git(repo, stage, *args):
    result = git_result(repo, stage, *args)
    if result.returncode:
        raise CheckError("git-" + stage + "-failed")
    return result.stdout


def resolve(repo, ref, label="ref"):
    sha = git(repo, "resolve-" + label, "rev-parse", "--verify",
              "--end-of-options", ref + "^{commit}").strip()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
        raise CheckError("invalid-commit-sha")
    return sha


def is_ancestor(repo, ancestor, descendant):
    result = git_result(repo, "ancestry", "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode not in (0, 1):
        raise CheckError("git-ancestry-failed")
    return result.returncode == 0


def valid_key(cfg, key):
    pattern = cfg.get("key_pattern", r"[A-Z][A-Z0-9_]*-[0-9]+")
    if not isinstance(key, str) or not re.fullmatch(pattern, key):
        raise CheckError("invalid-ticket-key; expected PROJ-123")


def marker(cfg, key):
    value = cfg.get("key_marker", "[{key}]")
    if not isinstance(value, str) or "{key}" not in value:
        raise CheckError("invalid-key-marker")
    return value.format(key=key)


def repo_path(cfg, value):
    path = Path(value)
    return path if path.is_absolute() else cfg.project_root / path


def check(args, report, cfg):
    keys = [args.key, *args.also_key]
    for key in keys:
        valid_key(cfg, key)
    repo = repo_path(cfg, args.repo)
    base = args.base or cfg.get("base_branch", "origin/main")
    report["base"] = base
    branch_head = resolve(repo, args.branch, "branch")
    base_head = resolve(repo, base, "base")
    report.update(branch_head=branch_head, base_head=base_head)
    merge = git_result(repo, "merge-base", "merge-base", base_head, branch_head)
    if merge.returncode == 1:
        report["reason"] = "no-common-base"
        return 1
    if merge.returncode:
        raise CheckError("git-merge-base-failed")
    fork = merge.stdout.strip()
    report["merge_base"] = fork
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", fork):
        raise CheckError("invalid-merge-base")
    if not is_ancestor(repo, fork, base_head):
        report["reason"] = "fork-not-in-base"
        return 1
    behind = git(repo, "behind-count", "rev-list", "--count", branch_head + ".." + base_head, "--").strip()
    if not behind.isdigit():
        raise CheckError("invalid-behind-count")
    report["behind"] = int(behind)
    history = git(repo, "commit-log", "log", "--no-show-signature", "-z",
                  "--format=%H%x00%P%x00%s", base_head + ".." + branch_head, "--")
    fields = history.split("\0") if history else []
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 3:
        raise CheckError("invalid-commit-log")
    markers = [marker(cfg, key) for key in keys]
    for offset in range(0, len(fields), 3):
        sha, parents, subject = fields[offset:offset + 3]
        reasons = []
        if len(parents.split()) > 1:
            reasons.append("merge-commit")
        if not any(value in subject for value in markers):
            reasons.append("subject-missing-allowed-ticket-marker")
        report["commits"].append({"sha": sha, "subject": subject,
                                  "ok": not reasons, "reason": "; ".join(reasons)})
    okay = all(commit["ok"] for commit in report["commits"])
    report["reason"] = "clean-ticket-history" if okay else "foreign-or-merge-commits"
    return 0 if okay else 1


def main(argv=None):
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--also-key", action="append", default=[])
    parser.add_argument("--base")
    report = {"check": "base", "status": "FAIL", "branch": None, "key": None,
              "commits": [], "merge_base": None, "base_head": None, "behind": None}
    try:
        args = parser.parse_args(argv)
        report.update(branch=args.branch, key=args.key)
        code = check(args, report, load_config(args.config))
    except (ValueError, OSError, KeyError, TypeError) as error:
        report["error"] = str(error) if isinstance(error, (CheckError, StateDirectoryError)) else "invalid-config"
        code = 2
    report["status"] = "PASS" if code == 0 else "FAIL"
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
