"""Verify completed CI evidence contains an expected commit, without fetching."""

import json
from pathlib import Path
import re
import subprocess

from .check_base import CheckError, Parser, is_ancestor, repo_path, resolve
from .config import load_config


MAX_JSON_BYTES = 8 * 1024 * 1024


def positive_integer(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise CheckError("invalid-build-number") from None
    if number <= 0:
        raise CheckError("invalid-build-number")
    return number


def field(data, path):
    if not isinstance(path, str) or not path:
        raise CheckError("invalid-ci-field")
    for key in path.split("."):
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def read_run(args, cfg):
    if args.run_json:
        try:
            with Path(args.run_json).open("rb") as source:
                raw = source.read(MAX_JSON_BYTES + 1).decode("utf-8")
        except (OSError, UnicodeError):
            raise CheckError("invalid-run-json") from None
    else:
        ci = cfg.get("ci", {})
        if not isinstance(ci, dict) or not ci.get("view_cmd"):
            raise CheckError("ci-view-command-missing")
        try:
            result = cfg.run(ci["view_cmd"], job=args.job, build=args.build, context=args.context)
        except (OSError, subprocess.SubprocessError):
            raise CheckError("ci-command-failed") from None
        if result.returncode:
            raise CheckError("ci-command-failed")
        raw = result.stdout
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_JSON_BYTES:
        raise CheckError("invalid-run-json")
    try:
        run = json.loads(raw)
    except (ValueError, TypeError):
        raise CheckError("invalid-run-json") from None
    if not isinstance(run, dict):
        raise CheckError("invalid-run-json")
    if "number" in run and (type(run["number"]) is not int or run["number"] != args.build):
        raise CheckError("build-mismatch")
    return run


def check(args, report, cfg):
    run = read_run(args, cfg)
    ci = cfg.get("ci", {})
    fields = ci.get("fields", {}) if isinstance(ci, dict) else None
    if not isinstance(fields, dict):
        raise CheckError("invalid-ci-fields")
    status = field(run, fields.get("status", "status"))
    result = field(run, fields.get("result", "result"))
    checkout = field(run, fields.get("commit", "commit"))
    report["result"] = result if isinstance(result, str) else None
    valid_checkout = isinstance(checkout, str) and re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", checkout)
    report["checkout_commit"] = checkout if valid_checkout else None
    if status != "completed":
        return 1, "not-completed"
    if result != "SUCCESS":
        return 1, "result-not-success"
    if checkout is None or checkout == "":
        return 1, "checkout-commit-missing"
    if not valid_checkout:
        return 1, "checkout-commit-invalid"
    repo = repo_path(cfg, args.repo)
    expected = resolve(repo, args.expect, "expected")
    report["expect_sha"] = expected
    try:
        actual = resolve(repo, checkout, "checkout")
    except CheckError as error:
        if str(error) == "git-resolve-checkout-failed":
            return 1, "checkout-commit-not-in-repo"
        raise
    report["checkout_commit"] = actual
    if not is_ancestor(repo, expected, actual):
        return 1, "expect-not-contained"
    report["contains_expect"] = True
    return 0, "contains-expect"


def main(argv=None):
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config")
    parser.add_argument("--job", required=True)
    parser.add_argument("--build", type=positive_integer, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--expect", required=True)
    parser.add_argument("--context", default="dev")
    parser.add_argument("--run-json")
    report = {"check": "deploy", "status": "FAIL", "job": None, "build": None,
              "result": None, "checkout_commit": None, "expect": None,
              "expect_sha": None, "contains_expect": False, "reason": None}
    try:
        args = parser.parse_args(argv)
        report.update(job=args.job, build=args.build, expect=args.expect)
        if not all(value.strip() for value in (args.job, args.repo, args.expect, args.context)):
            raise CheckError("invalid-arguments")
        code, reason = check(args, report, load_config(args.config))
    except (ValueError, OSError, KeyError, TypeError) as error:
        code, reason = 2, str(error) if isinstance(error, CheckError) else "invalid-config"
    report.update(status="PASS" if code == 0 else "FAIL", reason=reason)
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
