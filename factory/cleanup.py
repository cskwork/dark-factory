"""Remove the worktrees a worker listed as cleanup:<path>; dry-run unless --execute.

Workers never clean up themselves: a worker that finished its work stalled on a cleanup
command's confirmation prompt and returned no result. They list `cleanup:<absolute path>` in
artifacts and the orchestrator runs this command after check-result.

A target is removed only when it is a worktree (a `.git` file) under a configured
`[cleanup].roots` directory, with no uncommitted, untracked or unpushed changes. Removal is a
plain `git worktree remove` (never forced). When the worktree's branch starts with
`merge_branch_prefix` and its tip is contained in an `env_branches` ref, that branch is
deleted as well. Local refs are used; refresh them first.
Exit 0 when nothing failed, 1 when a removal failed, 2 for invalid input or configuration.
"""
import argparse
import json
from pathlib import Path
import subprocess
from .config import load_config


def git(path, *args, timeout=60):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=timeout)


def keep_reason(path):
    status = git(path, "status", "--porcelain")
    if status.returncode:
        return "status-failed"
    if status.stdout.strip():
        return "dirty"
    unpushed = git(path, "log", "--oneline", "HEAD", "--not", "--remotes")
    if unpushed.returncode:
        return "log-failed"
    return "unpushed" if unpushed.stdout.strip() else None


def targets(cfg, result):
    """Yield (path, reason to keep or None) for each cleanup:<path> artifact."""
    value = json.loads(Path(result).read_text())
    if not isinstance(value, dict) or not isinstance(value.get("artifacts"), list):
        raise ValueError("invalid worker result")
    roots = [cfg.resolve(root, cfg.project_root) for root in cfg.get("cleanup", {}).get("roots", [])]
    seen = set()
    for artifact in value["artifacts"]:
        if not isinstance(artifact, str) or not artifact.startswith("cleanup:"):
            continue
        raw = Path(artifact[len("cleanup:"):]).expanduser()
        if not raw.is_absolute():
            yield raw, "not-absolute"
            continue
        path = raw.resolve()
        if path in seen:
            continue
        seen.add(path)
        if not any(root in path.parents for root in roots):
            yield path, "outside-roots"
        elif not (path / ".git").is_file():
            yield path, "not-worktree"
        else:
            yield path, keep_reason(path)


def remove(path, prefix, refs):
    """Return (worktree error or None, promoted merge branch or None, branch error or None)."""
    common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode:
        return "common-dir-failed", None, None
    repo = common.stdout.strip()
    branch = git(path, "symbolic-ref", "-q", "--short", "HEAD").stdout.strip()
    tip = git(path, "rev-parse", "HEAD").stdout.strip()
    # Large build outputs can take minutes to delete.
    done = git(repo, "worktree", "remove", str(path), timeout=600)
    if done.returncode:
        return (done.stderr.strip().splitlines() or ["remove-failed"])[-1][:200], None, None
    promoted = branch.startswith(prefix) and tip and any(
        git(repo, "merge-base", "--is-ancestor", tip, ref).returncode == 0 for ref in refs)
    if not promoted:
        return None, None, None
    deleted = git(repo, "branch", "-D", branch)
    branch_error = None if deleted.returncode == 0 else "branch-delete-failed: " + branch
    return None, branch, branch_error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--result", type=Path, required=True, help="worker result JSON")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    out = {"mode": "execute" if args.execute else "dry-run", "removable": [], "removed": [],
           "kept": [], "failed": [], "branchesDeleted": []}
    try:
        cfg = load_config(args.config)
        prefix = cfg.get("cleanup", {}).get("merge_branch_prefix", "merge/")
        refs = [ref for ref in cfg.get("env_branches", {}).values() if isinstance(ref, str)]
        for path, reason in list(targets(cfg, args.result)):
            if reason:
                out["kept"].append({"path": str(path), "reason": reason})
                continue
            out["removable"].append(str(path))
            if args.execute:
                error, branch, branch_error = remove(path, prefix, refs)
                if error:
                    out["failed"].append({"path": str(path), "error": error})
                    continue
                out["removed"].append(str(path))
                if branch_error:
                    out["failed"].append({"path": str(path), "error": branch_error})
                elif branch:
                    out["branchesDeleted"].append(branch)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({**out, "error": type(error).__name__}))
        return 2
    print(json.dumps(out))
    return 1 if out["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
