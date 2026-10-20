"""Exit 0 only for established idle, 1 for work, 2 for invalid state or errors."""
import argparse
import json
import subprocess
from .config import load_config
from .state import read_object, write_json
from .triage import triage


def decide(cfg, classifier=None):
    result = {"idle": False, "reasons": [], "events": 0, "queue": {}}
    reasons = result["reasons"]
    try:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        # Always use this invocation's stdout; an old empty scan is never evidence.
        (cfg.state_dir / "scan.json").unlink(missing_ok=True)
        response = cfg.run(cfg.get("intake", {})["scan_cmd"])
        if response.returncode:
            reasons.append("scan-failed")
        if "unauthorized" in (response.stdout + response.stderr).lower():
            reasons.append("scan-unauthorized")
        scan = json.loads(response.stdout)
        if not isinstance(scan, dict) or not isinstance(scan.get("events"), list) or not isinstance(scan.get("errors"), list):
            raise ValueError("invalid scan")
        for event in scan["events"]:
            if (not isinstance(event, dict) or not cfg.valid_key(event.get("key"))
                    or not isinstance(event.get("id"), str) or not event["id"]
                    or not all(isinstance(event.get(name), str) for name in ("author", "text"))
                    or not all(type(event.get(name)) is bool for name in
                               ("mentions_me", "assigned_to_me", "answered_by_me_after"))):
                raise ValueError("invalid event")
        if scan["errors"]:
            reasons.append("scan-errors")
        if reasons:
            return result, 1
        events = triage(cfg, scan["events"], classifier)
        write_json(cfg.state_dir / "scan.json", {**scan, "events": events})
        result["events"] = len(events)
        if events:
            reasons.append("new-events")
        for key, item in read_object(cfg.state_dir / "queue.json").items():
            if not cfg.valid_key(key) or not isinstance(item, dict):
                raise ValueError("invalid queue")
            waiting = item.get("waiting")
            if waiting is not None and not isinstance(waiting, str):
                raise ValueError("invalid waiting")
            result["queue"][key] = waiting
            if waiting is None:
                reasons.append("queue-runnable:" + key)
            if waiting in ("env-human", "env-down") and item.get("needs"):
                from .preflight import check
                needs = item["needs"]
                if isinstance(needs, str):
                    needs = needs.split(",")
                if not isinstance(needs, list) or not all(isinstance(n, str) for n in needs):
                    raise ValueError("invalid probe needs")
                probe, code = check(cfg, needs)
                if code == 0:
                    reasons.append("environment-ready:" + key)
                elif code not in (10, 20):
                    raise ValueError("invalid probe result")
        for key, approval in read_object(cfg.state_dir / "approvals.json").items():
            if not isinstance(approval, dict):
                raise ValueError("invalid approval")
            if approval.get("approved") is True and not approval.get("used_at"):
                reasons.append("approval-unused:" + key)
        for path in cfg.state_dir.glob("decisions/*.json"):
            if read_object(path).get("wake_pending"):
                reasons.append("decision-wake-pending")
        if reasons:
            return result, 1
        result.update(idle=True, reasons=["no-events", "no-runnable-work"])
        return result, 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        reasons.append("internal-error")
        return result, 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    try:
        result, code = decide(load_config(args.config))
    except (OSError, ValueError):
        result, code = {"idle": False, "reasons": ["invalid-config"]}, 2
    print(json.dumps(result))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
