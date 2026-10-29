"""Exit 0 only for established idle, 1 for work, 2 for invalid state or errors."""
import argparse
from datetime import date
import hashlib
import json
import math
import re
import subprocess
import time
from .config import StateDirectoryError, load_config
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


def pending_focus(cfg, blocked):
    """Keep blocked requests for reporting; unscoped manual requests still wake."""
    path = cfg.state_dir / "pending-focus.jsonl"
    runnable, blocked_pending = False, set()
    for line in path.read_text().splitlines() if path.exists() else []:
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("invalid pending focus")
        key = row.get("key", row.get("focus"))
        if key is not None and not cfg.valid_key(key):
            raise ValueError("invalid pending focus key")
        if key is not None and ("*" in blocked or key in blocked):
            blocked_pending.add(key)
        else:
            runnable = True
    return runnable, sorted(blocked_pending)



SCAN_RETRY_PAUSE = 3
SCAN_ALERT_FAILURES = 3


def scan_budget(cfg):
    """Total wall seconds for one scan including its retry; a hang must not outlive the tick."""
    settings = cfg.get("intake", {})
    default = cfg.get("command_timeout_seconds", 300)
    budget = settings.get("scan_budget_seconds", default) if isinstance(settings, dict) else default
    if type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0:
        raise ValueError("invalid scan budget")
    return float(budget)


def record_scan_failure(cfg, state, now, count, failure, elapsed, cooldown=True):
    """Count every observed scan failure; only a command failure may cool the adapter down."""
    state.setdefault("lastSuccessAt", None)
    ladder = now + (1800 if count == 0 else 3600)
    state.update(status="unavailable", failureCount=count + 1, lastErrorAt=now, lastError=failure,
                 retryAt=ladder if cooldown else None, durationSeconds=elapsed,
                 cooldown=False, attemptStartedAt=None)
    if state["failureCount"] >= SCAN_ALERT_FAILURES and not state.get("alerted"):
        append_json(cfg.state_dir / "lessons.jsonl", {
            "at": now, "key": None, "kind": "infra_fail",
            "observation": "Intake scan failed " + str(state["failureCount"]) + " times in a row: " + failure,
            "evidence": str(cfg.state_dir / "scan-state.json"),
            "proposal": "Repair the intake adapter, then scan with --force-refresh"})
        state["alert"] = notify(cfg, "Intake scan unavailable: " + failure
                                + " (" + str(state["failureCount"]) + " in a row)")
        state["alerted"] = True
    write_json(cfg.state_dir / "scan-state.json", state)
    return state


def source_health(state, scan, now):
    """Per-source status is descriptive; one unavailable source never cools the adapter."""
    sources = scan.get("sources")
    if sources is None:
        state.pop("sources", None)
        return "unavailable" if scan["errors"] else "available"
    if not isinstance(sources, dict) or not sources:
        raise ValueError("invalid sources")
    previous = state.get("sources", {})
    if not isinstance(previous, dict) or not all(isinstance(row, dict) for row in previous.values()):
        raise ValueError("invalid previous source health")
    observed = {}
    for name, row in sources.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name)
                or not isinstance(row, dict) or row.get("status") not in ("available", "degraded", "unavailable")
                or ("reason" in row and (not isinstance(row["reason"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", row["reason"])))):
            raise ValueError("invalid source health")
        timing = row.get("durationSeconds")
        if timing is not None and (type(timing) not in (int, float) or not math.isfinite(timing) or timing < 0):
            raise ValueError("invalid source duration")
        history = previous.get(name, {})
        healthy = row["status"] == "available"
        observed[name] = {"status": row["status"], "durationSeconds": timing, "lastSuccessAt": now if healthy else history.get("lastSuccessAt"),
                          "lastErrorAt": history.get("lastErrorAt") if healthy else now,
                          "lastError": history.get("lastError") if healthy else row.get("reason", "source-unavailable")}
    state["sources"] = observed
    statuses = [row["status"] for row in observed.values()]
    return "available" if all(v == "available" for v in statuses) else ("unavailable" if all(v == "unavailable" for v in statuses) else "degraded")


def scan_intake(cfg, now, force_refresh=False):
    """Only whole-command failure cools down an adapter; sources are descriptive."""
    path = cfg.state_dir / "scan-state.json"
    state = read_object(path)
    count = state.get("failureCount", 0)
    if type(count) is not int or count < 0:
        raise ValueError("invalid scan failure count")
    attempt = state.get("attemptStartedAt")
    if attempt is not None:
        # A tick killed inside the scan never reached the accounting below, so the same
        # hang came back every tick with failureCount 0. Count it before scanning again.
        try:
            elapsed = max(0.0, now - epoch(attempt))
        except (ValueError, TypeError):
            elapsed = None
        state = record_scan_failure(cfg, state, now, count, "scan-interrupted", elapsed)
        return None, {**state, "cooldown": True}, ["scan-interrupted"]
    if not force_refresh and epoch(state.get("retryAt") or 0) > now:
        return None, {**state, "cooldown": True}, ["scan-cooldown"]
    budget = scan_budget(cfg)
    state.update(attemptStartedAt=now, cooldown=False)
    write_json(path, state)
    started = time.monotonic()
    try:
        failure, scan = None, None
        try:
            response = cfg.run(cfg.get("intake", {})["scan_cmd"], timeout=budget)
            if response.returncode:
                # Retry only what the budget still covers; a hang must not double the tick.
                remaining = budget - (time.monotonic() - started) - SCAN_RETRY_PAUSE
                if remaining > 0:
                    time.sleep(SCAN_RETRY_PAUSE)
                    response = cfg.run(cfg.get("intake", {})["scan_cmd"], timeout=remaining)
            if response.returncode:
                failure = "scan-failed"
            else:
                unauthorized = "unauthorized" in (response.stdout + response.stderr).lower()
                try:
                    scan = json.loads(response.stdout)
                except ValueError:
                    if not unauthorized:
                        raise
                # Structured source health scopes auth failure to its unavailable source.
                if unauthorized and not (isinstance(scan, dict) and "sources" in scan):
                    failure = "scan-unauthorized"
        except subprocess.TimeoutExpired:
            failure = "scan-timeout"
        except (OSError, subprocess.SubprocessError):
            failure = "scan-command-unavailable"
        elapsed = time.monotonic() - started
        if failure:
            return None, record_scan_failure(cfg, state, now, count, failure, elapsed), [failure]
        if not isinstance(scan, dict) or not isinstance(scan.get("events"), list) or not isinstance(scan.get("errors"), list):
            raise ValueError("invalid scan")
        for event in scan["events"]:
            if (not isinstance(event, dict) or not cfg.valid_key(event.get("key"))
                    or not isinstance(event.get("id"), str) or not event["id"]
                    or not all(isinstance(event.get(name), str) for name in ("author", "text"))
                    or not all(type(event.get(name)) is bool for name in
                               ("mentions_me", "assigned_to_me", "answered_by_me_after"))):
                raise ValueError("invalid event")
        status = source_health(state, scan, now)
        state.update(status=status, failureCount=0, retryAt=None, durationSeconds=elapsed,
                     cooldown=False, attemptStartedAt=None, alerted=False)
        if status == "available":
            state["lastSuccessAt"] = now
        else:
            state.update(lastErrorAt=now, lastError="source-unavailable" if scan.get("sources") else "scan-errors")
        for name in ("lastSuccessAt", "lastErrorAt", "lastError"):
            state.setdefault(name, None)
        write_json(path, state)
        return scan, state, ([] if status == "available" else ["scan-errors"])
    except Exception as error:
        # Any observed error resolves this attempt; only a killed process leaves the marker.
        # Malformed data still wakes the orchestrator, so count it: otherwise a fast garbage
        # response burns an expensive wake every tick with nothing recorded.
        label = "scan-malformed" if isinstance(error, (ValueError, TypeError, KeyError)) else "scan-error"
        try:
            record_scan_failure(cfg, state, now, count, label,
                                time.monotonic() - started, cooldown=False)
        except OSError:
            pass  # The original failure matters more than its bookkeeping.
        raise


def decide(cfg, classifier=None, force_refresh=False):
    result = {"idle": False, "reasons": [], "events": 0, "queue": {}, "stuck": [], "waitingHuman": [], "shouldWake": True, "timings": {}}
    now = time.time()
    reasons = result["reasons"]
    try:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        from .runtime import blocked_keys
        blocked = blocked_keys(cfg)
        result["blockedKeys"] = sorted(blocked)
        runnable_focus, result["blockedPendingKeys"] = pending_focus(cfg, blocked)
        if runnable_focus:
            reasons.append("pending-focus")
        # Always use this invocation's stdout; an old empty scan is never evidence.
        (cfg.state_dir / "scan.json").unlink(missing_ok=True)
        scan, health, monitoring = scan_intake(cfg, now, force_refresh)
        result["sourceHealth"] = health
        result["timings"]["scanSeconds"] = health.get("durationSeconds") if not health.get("cooldown") else 0
        reasons.extend(monitoring)
        scan_present = scan is not None
        scan = scan or {"events": [], "errors": []}
        triage_started = time.monotonic()
        events = triage(cfg, scan["events"], classifier) if not scan["errors"] or "sources" in scan else []
        result["timings"]["triageSeconds"] = time.monotonic() - triage_started
        result["timings"]["preflightSeconds"] = 0
        events = [event for event in events if "*" not in blocked and event["key"] not in blocked]
        if scan_present:
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
                    probe_started = time.monotonic()
                    cache[signature] = check(cfg, names, fail_fast=True)
                    result["timings"]["preflightSeconds"] += time.monotonic() - probe_started
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
        result["shouldWake"] = bool([reason for reason in reasons if reason not in monitoring])
        if reasons:
            return result, 1
        result.update(idle=True, shouldWake=False, reasons=["no-events", "no-runnable-work"])
        return result, 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        result["shouldWake"] = True
        reasons.append("internal-error")
        return result, 2
    finally:
        write_json(cfg.state_dir / "guard-last.json", {"at": time.time(), **result})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args(argv)
    try:
        result, code = decide(load_config(args.config), force_refresh=args.force_refresh)
    except StateDirectoryError as error:
        result, code = {"idle": False, "reasons": ["invalid-config"], "error": str(error)}, 2
    except (OSError, ValueError):
        result, code = {"idle": False, "reasons": ["invalid-config"]}, 2
    print(json.dumps(result))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
