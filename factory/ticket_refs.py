"""Find own or carrying branches by subject marker using existing local refs."""

import json
from pathlib import Path
import re

from .check_base import CheckError, Parser, git, is_ancestor, marker, repo_path, resolve, valid_key
from .config import load_config


def remote_prefix(base):
    if base.startswith("refs/remotes/"):
        return "refs/remotes/" + base.removeprefix("refs/remotes/").split("/", 1)[0] + "/"
    return "refs/remotes/" + (base.split("/", 1)[0] if "/" in base else "origin") + "/"


def scan_repo(repo, key, cfg):
    valid_key(cfg, key)
    base = cfg.get("base_branch", "origin/main")
    base_sha = resolve(repo, base, "base")
    environments = cfg.get("env_branches", {"dev": "origin/dev", "stg": "origin/stg", "prd": "origin/main"})
    if not isinstance(environments, dict) or not environments:
        raise CheckError("invalid-environment-branches")
    # Missing environment refs are errors, never evidence of absent deployment.
    env_shas = {name: resolve(repo, ref, "environment") for name, ref in environments.items()}
    prefixes = cfg.get("branch_prefixes", ["fix/", "feat/"])
    if not isinstance(prefixes, list) or not prefixes or any(not isinstance(p, str) or not p for p in prefixes):
        raise CheckError("invalid-branch-prefixes")
    remote = remote_prefix(base)
    listed = git(repo, "branch-list", "for-each-ref", "--format=%(refname)", remote)
    refs = [ref for ref in listed.splitlines() if any(ref.removeprefix(remote).startswith(p) for p in prefixes)]
    if not refs:
        return None
    tips = {ref: resolve(repo, ref, "branch") for ref in refs}
    history = git(repo, "ticket-log", "log", "--no-show-signature", "-z", "--format=%H%x00%s",
                  *tips.values(), "--not", base_sha, "--")
    fields = history.split("\0") if history else []
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        raise CheckError("invalid-ticket-log")
    wanted = marker(cfg, key)
    commits = [fields[i] for i in range(0, len(fields), 2) if wanted in fields[i + 1]]
    if not commits:
        return None
    # Key validation patterns may be anchored; the surrounding marker supplies
    # those boundaries when scanning a subject.
    pattern = cfg.get("key_pattern", r"[A-Z][A-Z0-9_]*-[0-9]+")
    pattern = pattern.removeprefix("^").removesuffix("$")
    template = cfg.get("key_marker", "[{key}]")
    marker_pattern = re.compile(re.escape(template).replace(re.escape("{key}"), "(?P<ticket_key>" + pattern + ")"))
    rows = []
    for ref, tip in tips.items():
        if not any(is_ancestor(repo, commit, tip) for commit in commits):
            continue
        subjects = git(repo, "branch-log", "log", "--no-show-signature", "--format=%s", base_sha + ".." + tip, "--")
        others = set()
        for subject in subjects.splitlines():
            for match in marker_pattern.finditer(subject):
                candidate = match.group("ticket_key")
                if candidate != key and marker(cfg, candidate) in subject:
                    others.add(candidate)
        branch = ref.removeprefix(remote)
        rows.append({"branch": branch, "own": branch in [p + key for p in prefixes], "other_keys": sorted(others)})
    env = {name: all(is_ancestor(repo, commit, sha) for commit in commits) for name, sha in env_shas.items()}
    return {"repo": Path(repo).name, "path": str(repo), "commits": commits,
            "branches": rows, "environments": env}


def discover_repos(cfg):
    patterns = cfg.get("repos", ["*"])
    if isinstance(patterns, str):
        patterns = [patterns]
    if not isinstance(patterns, list) or not patterns or any(not isinstance(p, str) or not p for p in patterns):
        raise CheckError("invalid-repository-patterns")
    repos = set()
    for pattern in patterns:
        if Path(pattern).is_absolute():
            raise CheckError("repository-pattern-must-be-relative")
        for path in cfg.project_root.glob(pattern):
            if (path / ".git").exists() or ((path / "HEAD").is_file() and (path / "objects").is_dir()):
                repos.add(path)
    if not repos:
        raise CheckError("no-repositories-found")
    return sorted(repos)


def main(argv=None):
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config")
    parser.add_argument("key")
    parser.add_argument("--repo", action="append", default=[])
    parser.add_argument("--root", help="override the configured project root")
    report = {"key": None, "status": "FAIL", "repos": []}
    try:
        args = parser.parse_args(argv)
        report["key"] = args.key
        cfg = load_config(args.config)
        valid_key(cfg, args.key)
        if args.root:
            cfg.project_root = Path(args.root).resolve()
        repos = [repo_path(cfg, path) for path in args.repo] or discover_repos(cfg)
        found = [row for row in (scan_repo(repo, args.key, cfg) for repo in repos) if row]
        status = "none" if not found else ("own-branch" if all(any(b["own"] for b in row["branches"]) for row in found) else "carried")
        report.update(status=status, repos=found)
        code = 0 if found else 1
    except (ValueError, OSError, KeyError, TypeError) as error:
        report["error"] = str(error) if isinstance(error, CheckError) else "invalid-config"
        code = 2
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
