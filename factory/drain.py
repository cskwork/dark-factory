"""Sequential runtime budgets, queue ranking, and serialized run bookkeeping."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import time
from .config import StateDirectoryError, load_config
from .state import BOOKKEEPING, item_digest, locked, read_object, transaction


def epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value)
        except ValueError:
            result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    else:
        raise ValueError("invalid timestamp")
    if not math.isfinite(result):
        raise ValueError("invalid timestamp")
    return result


def iso(now):
    return datetime.fromtimestamp(epoch(now), timezone.utc).isoformat()


def backoff_active(item, now):
    return "backoffUntil" in item and epoch(item["backoffUntil"]) > epoch(now)


def duration(value):
    match = re.fullmatch(r"(\d+)([smh]?)", str(value))
    if not match:
        raise ValueError("invalid timeout")
    seconds = int(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]
    if seconds <= 0:
        raise ValueError("invalid timeout")
    return seconds


def enabled(cfg):
    switch = cfg.state_dir / "drain"
    return cfg.get("drain", {}).get("enabled", False) is True and (
        not switch.exists() or switch.read_text().strip() != "0")


def run_filename(token, filename):
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise ValueError("invalid run token")
    return "runs/" + token + "/" + filename


def read_run(cfg, token):
    run_filename(token, "summary.json")
    value = read_object(cfg.state_dir / "running.json")
    if not value or value.get("token") != token:
        raise PermissionError("run ownership lost")
    return value


def started_at(run):
    return epoch(run.get("startedAt", run.get("started_at")))


@contextmanager
def owned(cfg, token):
    # Ownership and writes share the guard lock, so a reclaimed run cannot write.
    from .runtime import owns
    with locked(cfg.state_dir / ".guard.lock"):
        read_run(cfg, token)
        if not owns(cfg, token):
            raise PermissionError("run ownership lost")
        yield


def next_budget(cfg, token, default_timeout="90m", now=None, units=None):
    now = time.time() if now is None else epoch(now)
    data = read_run(cfg, token)
    if units is None:
        summary = read_object(cfg.state_dir / run_filename(token, "summary.json"))
        units = summary.get("runtimeUnitCount", 0)
    if type(units) is not int or units < 0:
        raise ValueError("invalid unit count")
    deadline = started_at(data) + 165 * 60
    remaining = deadline - now
    drain_enabled = enabled(cfg)
    timeout = max(0, min(duration(default_timeout), int(remaining - 600)))
    return dict(deadline=deadline, remainingSeconds=remaining, timeoutSeconds=timeout,
                timeout=str(timeout) + "s", canStart=remaining >= 1800 and (drain_enabled or units == 0),
                drainEnabled=drain_enabled)


def validate_queue(cfg, queue):
    if not isinstance(queue, dict):
        raise ValueError("invalid queue")
    for key, item in queue.items():
        if not cfg.valid_key(key) or not isinstance(item, dict):
            raise ValueError("invalid queue item")
        for field in ("stage", "waiting"):
            if field not in item:
                raise ValueError("missing queue field")
        if not isinstance(item["stage"], str) or not item["stage"]:
            raise ValueError("invalid queue stage")
        if item["waiting"] is not None and not isinstance(item["waiting"], str):
            raise ValueError("invalid waiting")
        if "commentIds" in item and (not isinstance(item["commentIds"], list)
                                      or any(not isinstance(v, str) for v in item["commentIds"])):
            raise ValueError("invalid commentIds")
    return queue


def pending_keys(directory):
    path = Path(directory) / "pending-focus.jsonl"
    if not path.exists():
        return []
    keys = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("invalid pending focus")
        key = row.get("key", row.get("focus"))
        if key is None:
            continue  # An unscoped manual request still wakes the guard.
        if not isinstance(key, str):
            raise ValueError("invalid pending focus")
        if key not in keys:
            keys.append(key)
    return keys


def rank(queue, *, approvals=None, handled=None, pending=(), focus=None,
         eligible=None, blocked=(), now=None):
    """Explicit eligibility admits recovered waits; backoff and key blocks still win."""
    now = time.time() if now is None else epoch(now)
    approvals, handled = approvals or {}, handled or {}
    allowed = None if eligible is None else set(eligible)
    blocked, pending = set(blocked), list(pending)

    def unused(key):
        approval = approvals.get(key, {})
        return isinstance(approval, dict) and approval.get("approved") is True and not approval.get("used_at")

    def stamp(value):
        return epoch(value) if value is not None else float("inf")

    def order(key):
        item = queue[key]
        category = 0 if unused(key) else 1 if key in pending else 2 if key == focus else 3
        comments = item.get("commentIds") or []
        entry = handled.get(key + ":" + comments[0], handled.get(comments[0], {})) if comments else {}
        suffix = re.search(r"(\d+)$", key)
        return (category, pending.index(key) if category == 1 else 0,
                stamp(item.get("createdAt")), stamp(entry.get("at")),
                int(suffix[0]) if suffix else float("inf"), key)

    keys = []
    for key, item in queue.items():
        if "*" in blocked or key in blocked or backoff_active(item, now) or (allowed is not None and key not in allowed):
            continue
        if unused(key) or key in pending or key == focus or (
                key in allowed if allowed is not None else item.get("waiting") is None):
            keys.append(key)
    return sorted(keys, key=order)


def summary_init(cfg, token, runnable, lane_eligible, now=None):
    now = time.time() if now is None else epoch(now)
    if any(type(value) is not int or value < 0 for value in (runnable, lane_eligible)):
        raise ValueError("invalid summary count")
    with owned(cfg, token):
        started = started_at(read_run(cfg, token))

        def change(value):
            if not value:
                value.update(token=token, startedAt=started, runnable=runnable,
                             laneEligible=lane_eligible, units=[], runtimeUnits=[],
                             runtimeUnitCount=0, workers=0, durationSeconds=0)
            value["updatedAt"] = iso(now)
        return transaction(cfg.state_dir, run_filename(token, "summary.json"), change)


def summary_record(cfg, token, key, unit, started, ended, workers, runtime=False):
    started, ended = epoch(started), epoch(ended)
    if (type(workers) is not int or workers < 0 or ended < started
            or type(runtime) is not bool or not isinstance(unit, str) or not unit
            or (key is not None and not cfg.valid_key(key))):
        raise ValueError("invalid unit record")
    record = dict(key=key, unit=unit, startedAt=started, endedAt=ended,
                  durationSeconds=ended - started, workers=workers, runtime=runtime)

    def change(value):
        if not value or not isinstance(value.get("units"), list):
            raise ValueError("summary-init required")
        identity = (key, unit, started)
        previous = next((row for row in value["units"]
                         if (row["key"], row["unit"], row["startedAt"]) == identity), None)
        if previous is not None:
            if previous != record:
                raise ValueError("conflicting unit record")
            return
        value["units"].append(record)
        value["runtimeUnits"] = [row for row in value["units"] if row["runtime"]]
        value["runtimeUnitCount"] = len(value["runtimeUnits"])
        value["workers"] = sum(row["workers"] for row in value["units"])
        value["durationSeconds"] = sum(row["durationSeconds"] for row in value["units"])
        value["updatedAt"] = iso(ended)

    with owned(cfg, token):
        return transaction(cfg.state_dir, run_filename(token, "summary.json"), change)


def update_item(cfg, token, key, action, now=None, reason=None):
    now = time.time() if now is None else epoch(now)
    if not cfg.valid_key(key) or action not in ("processed", "defer", "rate-limit"):
        raise ValueError("invalid queue action")
    if action == "rate-limit":
        reason = "rate-limit"
    if action != "processed" and (not isinstance(reason, str) or not reason):
        raise ValueError("defer reason required")
    with owned(cfg, token):
        started = started_at(read_run(cfg, token))

        def change(queue):
            if key not in queue or not isinstance(queue[key], dict):
                raise ValueError("missing queue key")
            item = queue[key]
            if not item.get("createdAt"):
                item["createdAt"] = iso(now)
            if action == "processed":
                item["deferCount"] = 0
                for field in ("deferredAt", "deferReason", "backoffUntil", "backoffLevel"):
                    item.pop(field, None)
            else:
                guard = read_object(cfg.state_dir / "guard-state.json")
                prior = guard.get("runnable", {}).get(key, {})
                count = item.get("deferCount", 0)
                if type(count) is not int or count < 0:
                    raise ValueError("invalid defer count")
                hash_changed = bool(prior.get("hash") and prior["hash"] != item_digest(item))
                if hash_changed:
                    count = 0
                same_run = item.get("deferredAt") is not None and epoch(item["deferredAt"]) >= started
                already_limited = (same_run and item.get("deferReason") == "rate-limit"
                                   and backoff_active(item, now))
                item["deferCount"] = count if not hash_changed and same_run else count + 1
                item.update(deferredAt=iso(now), deferReason=reason)
                if action == "rate-limit" and not already_limited:
                    old_level = item.get("backoffLevel", -1)
                    if type(old_level) is not int or old_level < -1:
                        raise ValueError("invalid backoff level")
                    level = min(old_level + 1, 2)
                    item.update(backoffLevel=level, backoffUntil=iso(now + min(1800 * 2 ** level, 7200)))
            item["updatedAt"] = iso(now)

        return transaction(cfg.state_dir, "queue.json", change)


def rate_limited(*paths):
    text = "\n".join(Path(path).read_text(encoding="utf-8", errors="replace") for path in paths if path is not None)
    return bool(re.search(r"rate[ -]?limit|\b429\b|\boverloaded\b", text, re.I))


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS)
    common.add_argument("--token", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)
    budget = sub.add_parser("next-budget", parents=[common])
    budget.add_argument("--default-timeout", default="90m")
    budget.add_argument("--units", "--runtime-units", type=int)
    budget.add_argument("--now", type=float)
    ranking = sub.add_parser("rank", parents=[common])
    ranking.add_argument("--focus")
    ranking.add_argument("--eligible", nargs="*")
    ranking.add_argument("--blocked", nargs="*", default=[])
    ranking.add_argument("--now", type=float)
    sub.add_parser("validate-queue", parents=[common])
    init = sub.add_parser("summary-init", parents=[common])
    init.add_argument("--runnable", type=int, required=True)
    init.add_argument("--lane-eligible", type=int, required=True)
    init.add_argument("--now", type=float)
    record = sub.add_parser("summary-record", parents=[common])
    record.add_argument("--key")
    record.add_argument("--unit", required=True)
    record.add_argument("--started-at", type=float, required=True)
    record.add_argument("--ended-at", type=float, required=True)
    record.add_argument("--workers", type=int, required=True)
    record.add_argument("--runtime", action="store_true")
    for name in ("defer", "processed", "rate-limit"):
        command = sub.add_parser(name, parents=[common])
        command.add_argument("--key", required=True)
        command.add_argument("--now", type=float)
        if name == "defer":
            command.add_argument("--reason", required=True)
    check = sub.add_parser("check-result", parents=[common])
    check.add_argument("--key", required=True)
    check.add_argument("--result", type=Path, required=True)
    check.add_argument("--stderr", type=Path)
    check.add_argument("--now", type=float)
    args = parser.parse_args(argv)
    try:
        cfg = load_config(getattr(args, "config", None))
        token = getattr(args, "token", None)
        now = getattr(args, "now", None)
        if args.command == "next-budget":
            result = next_budget(cfg, token, args.default_timeout, now, args.units)
        elif args.command in ("rank", "validate-queue"):
            queue = validate_queue(cfg, read_object(cfg.state_dir / "queue.json"))
            if args.command == "validate-queue":
                result = dict(valid=True, items=len(queue))
            else:
                from .runtime import blocked_keys
                blocked = set(args.blocked) | set(blocked_keys(cfg, token))
                result = rank(queue, approvals=read_object(cfg.state_dir / "approvals.json"),
                              handled=read_object(cfg.state_dir / "state.json").get("handled", {}),
                              pending=pending_keys(cfg.state_dir), focus=args.focus,
                              eligible=args.eligible, blocked=blocked, now=now)
        elif args.command == "summary-init":
            result = summary_init(cfg, token, args.runnable, args.lane_eligible, now)
        elif args.command == "summary-record":
            result = summary_record(cfg, token, args.key, args.unit, args.started_at,
                                    args.ended_at, args.workers, args.runtime)
        elif args.command == "check-result":
            with owned(cfg, token):
                limited = rate_limited(args.result, args.stderr)
            result = dict(rateLimited=limited, retryAllowed=not limited)
            if limited:
                update_item(cfg, token, args.key, "rate-limit", now)
                result.update(failureType="infra", deferReason="rate-limit")
        else:
            result = update_item(cfg, token, args.key, args.command, now, getattr(args, "reason", None))
        print(json.dumps(result))
        return 0
    except PermissionError:
        print("drain: run ownership lost", file=sys.stderr)
        return 1
    except StateDirectoryError as error:
        print("drain: " + str(error), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print("drain: invalid state or arguments", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
