"""Run configured prerequisite probes; report PASS, HUMAN (10), or DOWN (20)."""

import json
import subprocess

from .check_base import CheckError, Parser
from .config import StateDirectoryError, load_config


def _probe_order(probes, names):
    ordered, visited, visiting = [], set(), set()

    def visit(name):
        if name in visiting:
            raise CheckError("cyclic-preflight-requires")
        if name in visited:
            return
        visiting.add(name)
        for required in probes[name].get("requires", []):
            visit(required)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for name in names:
        visit(name)
    return ordered


def check_environment(cfg, needs=None, fail_fast=False):
    settings = cfg.get("preflight", {})
    probes = settings.get("probes", {}) if isinstance(settings, dict) else None
    if not isinstance(probes, dict) or not probes:
        raise CheckError("preflight-probes-missing")
    if isinstance(needs, str):
        raise CheckError("unknown-preflight-probe")
    names = list(probes) if needs is None else list(needs)
    if not names or any(not isinstance(name, str) or not name or name not in probes for name in names):
        raise CheckError("unknown-preflight-probe")
    if type(fail_fast) is not bool:
        raise CheckError("invalid-fail-fast")
    # Validate the whole configuration, including unrequested probes, before execution.
    for name, probe in probes.items():
        if not isinstance(name, str) or not name or not isinstance(probe, dict) or not probe.get("cmd"):
            raise CheckError("invalid-preflight-probe")
        codes = probe.get("human_exit_codes", [])
        if not isinstance(codes, list) or any(type(code) is not int or code == 0 for code in codes):
            raise CheckError("invalid-human-exit-codes")
        requires = probe.get("requires", [])
        if not isinstance(requires, list) or any(not isinstance(required, str) or not required
                                                  or required not in probes for required in requires):
            raise CheckError("invalid-preflight-requires")
        cfg.command(probe["cmd"])
    _probe_order(probes, probes)
    names = _probe_order(probes, names)
    results, by_name, stopped = [], {}, False
    for name in names:
        probe = probes[name]
        failed_requires = list(dict.fromkeys(required for required in probe.get("requires", [])
                                             if by_name[required]["status"] != "PASS"))
        if stopped or failed_requires:
            row = {"name": name, "status": "SKIP", "exit_code": None,
                   "reason": "fail-fast" if stopped else "dependency-failed", "human_action": None}
            if not stopped:
                row["failed_requires"] = failed_requires
            results.append(row)
            by_name[name] = row
            continue
        try:
            completed = cfg.run(probe["cmd"])
            code = completed.returncode
            status = "PASS" if code == 0 else ("HUMAN" if code in probe.get("human_exit_codes", []) else "DOWN")
            reason = "ok" if code == 0 else "command-failed"
        except subprocess.TimeoutExpired:
            code, status, reason = None, "DOWN", "timeout"
        except OSError:
            code, status, reason = None, "HUMAN", "command-unavailable"
        row = {"name": name, "status": status, "exit_code": code, "reason": reason,
               "human_action": probe.get("human_action") if status == "HUMAN" else None}
        results.append(row)
        by_name[name] = row
        stopped = fail_fast and status != "PASS"
    statuses = {row["status"] for row in results}
    status, code = ("HUMAN", 10) if "HUMAN" in statuses else (("DOWN", 20) if "DOWN" in statuses else ("PASS", 0))
    return {"check": "preflight", "status": status, "results": results}, code


def check(cfg, needs=None, fail_fast=False):
    return check_environment(cfg, needs, fail_fast)


def main(argv=None):
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config")
    parser.add_argument("--need", help="comma-separated probe names; default: all configured probes")
    parser.add_argument("--fail-fast", action="store_true", help="skip remaining probes after the first failure")
    try:
        args = parser.parse_args(argv)
        needs = args.need.split(",") if args.need is not None else None
        report, code = check_environment(load_config(args.config), needs, args.fail_fast)
    except (ValueError, OSError, KeyError, TypeError) as error:
        report = {"check": "preflight", "status": "FAIL", "results": [],
                  "error": str(error) if isinstance(error, (CheckError, StateDirectoryError)) else "invalid-config"}
        code = 2
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
