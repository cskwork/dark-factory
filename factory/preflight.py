"""Run configured prerequisite probes; report PASS, HUMAN (10), or DOWN (20)."""

import json
import subprocess

from .check_base import CheckError, Parser
from .config import load_config


def check_environment(cfg, needs=None):
    settings = cfg.get("preflight", {})
    probes = settings.get("probes", {}) if isinstance(settings, dict) else None
    if not isinstance(probes, dict) or not probes:
        raise CheckError("preflight-probes-missing")
    names = list(probes) if needs is None else list(dict.fromkeys(needs))
    if not names or any(not name or name not in probes for name in names):
        raise CheckError("unknown-preflight-probe")
    # Validate every requested probe before starting any commands.
    for name in names:
        probe = probes[name]
        if not isinstance(probe, dict) or not probe.get("cmd"):
            raise CheckError("invalid-preflight-probe")
        codes = probe.get("human_exit_codes", [])
        if not isinstance(codes, list) or any(type(code) is not int or code == 0 for code in codes):
            raise CheckError("invalid-human-exit-codes")
        cfg.command(probe["cmd"])
    results = []
    for name in names:
        probe = probes[name]
        try:
            completed = cfg.run(probe["cmd"])
            code = completed.returncode
            status = "PASS" if code == 0 else ("HUMAN" if code in probe.get("human_exit_codes", []) else "DOWN")
            reason = "ok" if code == 0 else "command-failed"
        except subprocess.TimeoutExpired:
            code, status, reason = None, "DOWN", "timeout"
        except OSError:
            code, status, reason = None, "HUMAN", "command-unavailable"
        results.append({"name": name, "status": status, "exit_code": code, "reason": reason,
                        "human_action": probe.get("human_action") if status == "HUMAN" else None})
    statuses = {row["status"] for row in results}
    status, code = ("HUMAN", 10) if "HUMAN" in statuses else (("DOWN", 20) if "DOWN" in statuses else ("PASS", 0))
    return {"check": "preflight", "status": status, "results": results}, code


def check(cfg, needs=None):
    return check_environment(cfg, needs)


def main(argv=None):
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config")
    parser.add_argument("--need", help="comma-separated probe names; default: all configured probes")
    try:
        args = parser.parse_args(argv)
        needs = args.need.split(",") if args.need is not None else None
        report, code = check_environment(load_config(args.config), needs)
    except (ValueError, OSError, KeyError, TypeError) as error:
        report = {"check": "preflight", "status": "FAIL", "results": [],
                  "error": str(error) if isinstance(error, CheckError) else "invalid-config"}
        code = 2
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
