"""Exit 0 only for established idle, 1 for work, 2 for invalid state or errors."""
import argparse
from datetime import date
import hashlib
import json
import subprocess
import time
from .config import load_config
from .state import BOOKKEEPING, append_json, item_digest, read_object, transaction, write_json
from .drain import backoff_active, epoch
from .triage import triage


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def normalize(item):
    """Local verification has no remote prerequisites; persist that decision."""
    if item.get("waiting") in ("env-human", "env-down"):
        step = item.get("nextStep") or ("local" if item.get("stage") == "tobe" else None)
        if step:
            item["nextStep"] = step
        if step == "local":
            item.update(needs="", waiting=None)


def previous_workers(directory, guard_at):
    """Only the latest run since the previous guard can justify deferring work."""
    if not guard_at:
        return 0
    candidates = []
    for path in (directory / "runs").glob("*"):
        if not path.is_dir():
            continue
        summary = read_object(path / "summary.json")
        started = summary.get("startedAt", summary.get("started_at"))
        observed = epoch(started) if started is not None else path.stat().st_mtime
        if observed >= int(epoch(guard_at)):
            candidates.append((observed, summary))
    if not candidates:
        return 0
    workers = max(candidates, key=lambda row: row[0])[1].get("workers", 0)
    if type(workers) is not int or workers < 0:
        raise ValueError("invalid workers count")
    return workers


def notify(cfg, message):
    command = cfg.get("notify", {}).get("cmd")
    if not command:
        return "disabled"
    try:
        return "sent" if cfg.run(command, message=message).returncode == 0 else "failed"
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return "failed"


def decide(cfg, classifier=None):
    result = {"idle": False, "reasons": [], "events": 0, "queue": {}, "stuck": [], "waitingHuman": []}
    now = time.time()
    reasons = result["reasons"]
    try:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        from .runtime import blocked_keys
        blocked = blocked_keys(cfg)
        result["blockedKeys"] = sorted(blocked)
        pending = cfg.state_dir / "pending-focus.jsonl"
        if pending.exists() and pending.read_text().strip():
            reasons.append("pending-focus")
        # Always use this invocation's stdout; an old empty scan is never evidence.
        (cfg.state_dir / "scan.json").unlink(missing_ok=True)
        response = cfg.run(cfg.get("intake", {})["scan_cmd"])
        if response.returncode:
            time.sleep(3)
            response = cfg.run(cfg.get("intake", {})["scan_cmd"])
        if response.returncode:
            reasons.append("scan-failed")
        if "unauthorized" in (response.stdout + response.stderr).lower():
            reasons.append("scan-unauthorized")
        if "scan-failed" in reasons or "scan-unauthorized" in reasons:
            return result, 1
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
            return result, 1
        events = triage(cfg, scan["events"], classifier)
        events = [event for event in events if "*" not in blocked and event["key"] not in blocked]
        write_json(cfg.state_dir / "scan.json", {**scan, "events": events})
        result["events"] = len(events)
        if events:
            reasons.append("new-events")
        queue = read_object(cfg.state_dir / "queue.json")
        originals = json.loads(json.dumps(queue))
        for key, item in queue.items():
            if not cfg.valid_key(key) or not isinstance(item, dict):
                raise ValueError("invalid queue")
            if item.get("waiting") is not None and not isinstance(item["waiting"], str):
                raise ValueError("invalid waiting")
            if "*" not in blocked and key not in blocked and not backoff_active(item, now):
                normalize(item)
        state = read_object(cfg.state_dir / "guard-state.json")
        previous = state.get("runnable", {})
        if not isinstance(previous, dict):
            raise ValueError("invalid guard state")
        for row in previous.values():
            if (not isinstance(row, dict) or not isinstance(row.get("hash"), str)
                    or type(row.get("count")) is not int or row["count"] < 1):
                raise ValueError("invalid runnable state")
        workers = previous_workers(cfg.state_dir, state.get("at"))
        notifications = state.get("waitingHuman", {})
        if (not isinstance(notifications, dict) or not isinstance(notifications.get("hashes", []), list)
                or not all(isinstance(value, str) for value in notifications.get("hashes", []))):
            raise ValueError("invalid notification state")
        runnable, cache, groups = {}, {}, {}
        for key, item in queue.items():
            waiting = item.get("waiting")
            result["queue"][key] = waiting
            if backoff_active(item, now):
                if key in previous:
                    runnable[key] = previous[key]
                continue
            if "*" in blocked or key in blocked:
                continue
            reason = "queue-runnable:" + key if waiting is None else None
            if waiting in ("env-human", "env-down") and item.get("needs"):
                from .preflight import check
                needs = item["needs"]
                if isinstance(needs, str):
                    needs = needs.split(",")
                if not isinstance(needs, list) or not all(isinstance(n, str) and n.strip() for n in needs):
                    raise ValueError("invalid probe needs")
                names = list(dict.fromkeys(n.strip() for n in needs))
                signature = tuple(sorted(names))
                if signature not in cache:
                    cache[signature] = check(cfg, names, fail_fast=True)
                probe, code = cache[signature]
                if code == 0:
                    reason = "environment-ready:" + key
                elif code not in (10, 20):
                    raise ValueError("invalid probe result")
                elif waiting == "env-human":
                    failed = next((row for row in probe.get("results", [])
                                   if row.get("status") in ("HUMAN", "DOWN")), {})
                    action = failed.get("human_action") or failed.get("reason") or "environment-unavailable"
                    groups.setdefault(action, []).append(key)
            if reason:
                digest = item_digest(item)
                old = previous.get(key, {})
                same = old.get("hash") == digest
                defer_count = item.get("deferCount", 0)
                if type(defer_count) is not int or defer_count < 0:
                    raise ValueError("invalid defer count")
                if not same and old and defer_count:
                    item["deferCount"] = defer_count = 0
                exempt = (workers >= 1 and bool(state.get("at"))
                          and item.get("deferredAt") is not None
                          and epoch(item["deferredAt"]) > epoch(state["at"]) and defer_count < 4)
                stuck = (same and not exempt) or defer_count >= 4
                row = {"hash": digest, "count": old.get("count", 0) + 1 if stuck else 1}
                runnable[key] = row
                if stuck:
                    result["stuck"].append("stuck-runnable:" + key)
                    if not old.get("reported"):
                        append_json(cfg.state_dir / "lessons.jsonl", {
                            "at": time.time(), "key": key, "kind": "infra_fail",
                            "observation": "Runnable item unchanged after orchestrator wake",
                            "evidence": digest, "proposal": "Execute nextStep or update needs/waiting"})
                        row["notify"] = notify(cfg, "Stuck runnable: " + key)
                    else:
                        row["notify"] = old.get("notify", "disabled")
                    row["reported"] = True
                    result.setdefault("stuckNotify", {})[key] = row["notify"]
                else:
                    reasons.append(reason)
        changes = {key: (originals[key], item) for key, item in queue.items() if item != originals[key]}
        if changes:
            def update_queue(latest):
                for key, (before, after) in changes.items():
                    current = latest.get(key)
                    if not isinstance(current, dict) or item_digest(current) != item_digest(before):
                        continue
                    for field, value in after.items():
                        if before.get(field) != value and current.get(field) == before.get(field):
                            current[field] = value
            transaction(cfg.state_dir, "queue.json", update_queue)
        state["runnable"] = runnable
        result["waitingHuman"] = [{"action": action, "keys": sorted(keys)} for action, keys in sorted(groups.items())]
        today = date.today().isoformat()
        seen = notifications.get("hashes", []) if notifications.get("day") == today else []
        if result["waitingHuman"]:
            digest = content_hash(result["waitingHuman"])
            if digest not in seen:
                message = "\n".join(row["action"] + ": " + ", ".join(row["keys"]) for row in result["waitingHuman"])
                result["notify"] = notify(cfg, message)
                seen.append(digest)
            else:
                result["notify"] = "deduplicated"
        state["waitingHuman"] = {"day": today, "hashes": seen}
        state["at"] = now
        transaction(cfg.state_dir, "guard-state.json", lambda latest: latest.update(
            runnable=state["runnable"], waitingHuman=state["waitingHuman"], at=state["at"]))
        for key, approval in read_object(cfg.state_dir / "approvals.json").items():
            if not isinstance(approval, dict):
                raise ValueError("invalid approval")
            if "*" in blocked or key in blocked or backoff_active(queue.get(key, {}), now):
                continue
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
    finally:
        write_json(cfg.state_dir / "guard-last.json", {"at": time.time(), **result})


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
