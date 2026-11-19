"""Export a local schema v1 board snapshot from portable factory state."""
import argparse
from collections import Counter
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import fcntl
from heapq import nlargest
import json
import os
from pathlib import Path
import re
import sys
import time

from .config import load_config
from .drain import epoch
from .lessons import FIXES
from .state import read_object, write_json


BOARD_TZ = timezone(timedelta(hours=9))
HISTORY_SECONDS = 7 * 86400
HISTORY_LIMIT = 300
DEFAULT_OUT = "~/.sdlc/todo-feed.json"

DELIVERY = {
    **dict.fromkeys(("dev-deployed", "dev-verified", "result-posted"), ("deliver", "dev")),
    **dict.fromkeys(("stg-ready", "stg-merged", "stg-deployed", "audit-verified", "audit-commented"),
                    ("deliver", "review")),
    "shipped": ("done", "production"),
}
STAGES = {
    **DELIVERY,
    "intent": ("intent", "Intent"),
    "intake": ("intent", "Intent"),
    "reproduce": ("intent", "Intent"),
    "triage": ("intent", "Intent"),
    "asis": ("plan", "Plan"),
    "as-is": ("plan", "Plan"),
    "plan": ("plan", "Plan"),
    "planning": ("plan", "Plan"),
    "planned": ("plan", "Plan"),
    "plan-ready": ("plan", "Plan"),
    "plan-review": ("plan", "Plan"),
    "plan-approved": ("plan", "Plan"),
    "design": ("plan", "Plan"),
    "designed": ("plan", "Plan"),
    "build": ("build", "Build"),
    "built": ("build", "Build"),
    "tobe": ("evidence", "Evidence"),
    "to-be": ("evidence", "Evidence"),
    "evidence": ("evidence", "Evidence"),
    "evidence-local": ("evidence", "Evidence"),
    "verify": ("evidence", "Evidence"),
    "verification": ("evidence", "Evidence"),
    "deliver": ("deliver", "Delivery"),
    "delivery": ("deliver", "Delivery"),
    "promotion": ("deliver", "review"),
    "dev": ("deliver", "dev"),
    "dev-delivery": ("deliver", "dev"),
    "dev-prep": ("deliver", "dev"),
    "dev-ready": ("deliver", "dev"),
    "deploy-dev": ("deliver", "dev"),
    "dev-deploy": ("deliver", "dev"),
    "dev-e2e": ("deliver", "dev"),
    "stg": ("deliver", "review"),
    "staging": ("deliver", "review"),
    "stg-deploy": ("deliver", "review"),
    "staging-deploy": ("deliver", "review"),
    "deploy-stg": ("deliver", "review"),
    "stg-e2e": ("deliver", "review"),
    "audit": ("deliver", "review"),
    "audit-smoke": ("deliver", "review"),
    "audit-e2e": ("deliver", "review"),
    "review": ("deliver", "review"),
    "closed": ("done", "production"),
    "done": ("done", "Done"),
}
WAITS = {
    "human-risk": ("you", "Human decision required"),
    "stg-approval": ("you", "Staging approval required"),
    "reporter": ("others", "Awaiting reporter"),
    "env-human": ("blocked", "Environment requires human action"),
    "env-down": ("blocked", "Environment unavailable"),
}
NEXT_STEPS = {
    "local", "dev", "dev-deploy", "dev-e2e", "stg", "staging", "stg-deploy", "stg-e2e",
    "audit", "audit-smoke", "audit-e2e", "prd", "production",
}
MONITOR_STATES = {
    "available": ("Available", "ok"),
    "degraded": ("Degraded", "warn"),
    "unavailable": ("Unavailable", "bad"),
}
LESSON_KINDS = {
    "infra_fail": "Infrastructure failure",
    "human_risk": "Human decision",
    "verifier_fail": "Verification failure",
    "reviewer_note": "Reviewer note",
    "worker_fail": "Worker failure",
    "review_rejected": "Review rejection",
}
# Export machine identifiers rather than arbitrary text. Free-form queue notes,
# actions, prompts, source errors and logs can contain confidential content.
LABEL = re.compile(r"[a-z][a-z0-9]{0,15}(?:-[a-z0-9]{1,16}){0,3}")
KEY = re.compile(r"[A-Za-z0-9]{1,16}(?:[-_.][A-Za-z0-9]{1,16}){0,3}")
PRIVATE_LABEL = re.compile(
    r"(?:^|-)(?:bearer|token|secret|password|passwd|authorization|api-?key|private-key|"
    r"credential|ghp|github-pat|xox[baprs]|sk|akia|asia)(?:-|$)"
)


def safe_label(value):
    if isinstance(value, str) and LABEL.fullmatch(value) and not PRIVATE_LABEL.search(value):
        return value
    return None


def _key(cfg, value):
    return (isinstance(value, str) and KEY.fullmatch(value) is not None
            and not PRIVATE_LABEL.search(re.sub(r"[_.]", "-", value.lower())) and cfg.valid_key(value))


def _object(value):
    return value if isinstance(value, dict) else {}


def _object_file(path):
    try:
        return read_object(path)
    except (OSError, ValueError, RecursionError):
        return {}


def _timestamp(value):
    try:
        return datetime.fromtimestamp(epoch(value), BOARD_TZ)
    except (OSError, ValueError, OverflowError):
        return None


def _iso(value):
    stamp = _timestamp(value)
    return stamp.isoformat(timespec="seconds") if stamp else None


def _mtime(path):
    try:
        if not path.is_symlink() and path.is_file():
            return path.stat().st_mtime
    except OSError:
        pass
    return 0


def _folders(root):
    try:
        if not root.is_symlink():
            for folder in root.iterdir():
                if not folder.is_symlink() and folder.is_dir():
                    yield folder
    except OSError:
        pass


def _recent(now, value):
    return value is not None and now - timedelta(seconds=HISTORY_SECONDS) <= value <= now


def _outcome(unit):
    invocation = _object(unit.get("invocation"))
    worker = _object(unit.get("worker"))
    outcome = invocation.get("outcome", worker.get("outcome", unit.get("outcome")))
    exit_code = invocation.get("exitCode", unit.get("exitCode"))
    if type(exit_code) is int and exit_code != 0:
        return "fail"
    if outcome in ("success", "ok", "done", "completed"):
        return "ok"
    if outcome in ("failed", "fail"):
        return "fail"
    return "unknown"


def _runs(cfg, now, owner=None):
    from .runtime import run_folder

    root = cfg.state_dir / "runs"
    until = now.timestamp()
    since = until - HISTORY_SECONDS

    def summaries():
        for folder in _folders(root):
            path = folder / "summary.json"
            modified = _mtime(path)
            if since <= modified <= until and run_folder(cfg, folder, {owner}):
                yield modified, path

    rows = []
    for _, path in nlargest(HISTORY_LIMIT, summaries()):
        summary = _object_file(path)
        units = summary.get("units", summary.get("runtimeUnits"))
        if not isinstance(units, list):
            continue
        for value in units:
            unit = _object(value)
            started = _timestamp(unit.get("startedAt"))
            ended = _timestamp(unit.get("endedAt"))
            key = unit.get("key")
            if not _recent(now, started):
                continue
            # A measured worker duration can be valid even if the wall clock moved backward.
            labels = [STAGES[name][1] for name in (unit.get("stage"), unit.get("unit"))
                      if isinstance(name, str) and name in STAGES]
            rows.append((started, {
                "key": key if _key(cfg, key) else None,
                "label": labels[0] if labels else "Work unit",
                "startedAt": started.isoformat(timespec="seconds"),
                "endedAt": ended.isoformat(timespec="seconds") if ended and ended <= now else None,
                "outcome": _outcome(unit),
            }))
    rows.sort(key=lambda pair: pair[0], reverse=True)
    today = [row for stamp, row in rows if stamp.date() == now.date()]
    return [row for _, row in reversed(rows[:HISTORY_LIMIT])], today


def _live_pid(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def _owned_attempts(cfg, running, now):
    from .runtime import valid_token

    token = running.get("token")
    if valid_token(token):
        folder = cfg.state_dir / "runs" / token
        if folder.is_symlink():
            return set()
        root = folder / "attempts"
    else:
        root = cfg.state_dir / "worker-attempts"
    keys = set()
    for path in _folders(root):
        if (path / "invocation.json").is_file():
            continue
        active = _object_file(path / "active.json")
        key = active.get("key")
        if _key(cfg, key) and _recent(now, _timestamp(active.get("startedAt"))) and _live_pid(active.get("pid")):
            keys.add(key)
    return keys


def _running(cfg, current, now):
    from .runtime import liveness_enabled, valid_token

    if current:
        started = _timestamp(current.get("started_at"))
    else:
        started = None
        with suppress(OSError, ValueError):
            started = _timestamp((cfg.state_dir / "running").read_text())
    if started is None:
        return False
    if valid_token(current.get("token")):
        with suppress(OSError, ValueError):
            if liveness_enabled(cfg):
                return True  # The guard retains ownership until a liveness check can revoke it.
    return 0 <= (now - started).total_seconds() < cfg.get("lock_stale_seconds", 10800)


def _stage_display(item, done_stages):
    delivery = item.get("delivery")
    if isinstance(delivery, str) and delivery in DELIVERY:
        return DELIVERY[delivery]
    raw_stage = item.get("stage")
    stage = raw_stage.strip().lower().replace("_", "-") if isinstance(raw_stage, str) else ""
    if delivery == "none" and (stage in DELIVERY or stage == "closed"):
        return None, safe_label(raw_stage)
    column, chip = STAGES.get(stage, (None, safe_label(raw_stage)))
    if raw_stage in done_stages and column != "done":
        return "done", "Done"
    return column, chip


def _items(cfg, queue, active):
    items = {}
    done_stages = cfg.get("reconcile", {}).get("done_stages", [])
    for key, value in sorted(queue.items()):
        if not _key(cfg, key) or not isinstance(value, dict):
            continue
        column, chip = _stage_display(value, done_stages)
        waiting = value.get("waiting")
        turn, waiting_text = None, None
        if isinstance(waiting, str) and waiting:
            turn, waiting_text = WAITS.get(waiting, (None, "Waiting"))
        if waiting_text is None and key in active:
            turn = "agent"
        step = value.get("nextStep")
        service = value.get("service")
        services = service.split(",") if isinstance(service, str) else []
        items[key] = {
            "slug": safe_label(value.get("slug")) or safe_label(key),
            "column": column,
            "chip": chip,
            "turn": turn,
            "waitingText": waiting_text,
            "service": ", ".join(part.strip() for part in services if safe_label(part.strip())) or None,
            "nextStep": step if isinstance(step, str) and step in NEXT_STEPS else None,
            "updatedAt": _iso(value.get("updatedAt")) or _iso(value.get("createdAt")),
        }
    return items


def _lesson_counts(path, now, days):
    since = now - timedelta(days=days)
    recent = 0
    counts = Counter()
    try:
        with path.open("rb") as stream:
            for line in stream:
                try:
                    value = _object(json.loads(line))
                except (ValueError, RecursionError):
                    continue
                kind = value.get("kind")
                if not isinstance(kind, str) or kind not in LESSON_KINDS:
                    continue
                counts[kind] += 1
                at = _timestamp(value.get("at"))
                if at and since <= at <= now:
                    recent += 1
    except OSError:
        pass
    return recent, counts


def _row(label, value, level=None):
    return {"label": label, "value": value, "level": level}


def _monitor(scan, tick):
    rows, alerts = [], []
    sources = [(name, source) for name, source in sorted(_object(scan.get("sources")).items())
               if safe_label(name)]
    failing = any(_object(source).get("status") in ("degraded", "unavailable") for _, source in sources)
    for name, data in [("Intake", scan), *sources]:
        data = _object(data)
        status = data.get("status")
        if not isinstance(status, str) or status not in MONITOR_STATES:
            continue
        value, level = MONITOR_STATES[status]
        success = _iso(data.get("lastSuccessAt"))
        if success is not None:
            value += " · " + success
        rows.append(_row(name, value, level))
        if level != "ok" and (name != "Intake" or not failing or data.get("failureCount")):
            alerts.append({"level": level, "text": name + " monitoring " + status,
                           "since": _iso(data.get("lastErrorAt")) or tick})
    return rows or [_row("Intake", "Unknown")], alerts


def snapshot(cfg, *, now=None, guard_failed=False):
    now = datetime.now(BOARD_TZ) if now is None else _timestamp(now)
    directory = cfg.state_dir
    guard = _object_file(directory / "guard-last.json")
    running = _object_file(directory / "running.json")
    idle = _object_file(directory / "idle.json")
    scan = _object_file(directory / "scan-state.json") or _object(guard.get("sourceHealth"))
    is_running = _running(cfg, running, now)
    active = _owned_attempts(cfg, running, now) if is_running or not running else set()
    items = _items(cfg, _object_file(directory / "queue.json"), active)
    token = running.get("token")
    runs, today = _runs(cfg, now, token if isinstance(token, str) else None)
    tick_at = _timestamp(guard.get("at"))
    tick = tick_at.isoformat(timespec="seconds") if tick_at else None
    monitors, alerts = _monitor(scan, tick)
    if guard_failed or guard.get("internalErrors"):
        since = now.isoformat(timespec="seconds") if guard_failed else tick
        alerts.append({"level": "bad", "text": "Guard check failed", "since": since})
    if running.get("livenessAlertAt"):
        alerts.append({"level": "warn", "text": "Run owner could not be confirmed",
                       "since": _iso(running["livenessAlertAt"])})
    stuck = guard.get("stuck", idle.get("stuck"))
    if isinstance(stuck, list) and stuck:
        alerts.append({"level": "warn", "text": f"{len(stuck)} unchanged runnable items", "since": tick})

    if is_running or active:
        state, text = "running", "Factory run in progress"
    elif not directory.is_dir():
        state, text = "off", "Factory state unavailable"
    else:
        state, text = "idle", "No active factory run"
    lessons, counts = _lesson_counts(directory / "lessons.jsonl", now, cfg.get("lessons", {}).get("window_days", 14))
    fixes = _object_file(directory / FIXES)
    failures = sum(run["outcome"] == "fail" for run in today)
    return {
        "version": 1,
        "source": "dark-factory",
        "generatedAt": now.isoformat(timespec="seconds"),
        "status": {"state": state, "text": text, "lanes": {"busy": int(state == "running"), "total": 1},
                   "nextRunAt": None},
        "alerts": alerts,
        "items": items,
        "runs": runs,
        "health": [
            {"title": "Monitoring", "rows": monitors},
            {"title": "Last tick", "rows": [_row("Time", tick_at.strftime("%m-%d %H:%M") if tick_at else "Unknown")]},
            {"title": "Lessons", "rows": [
                _row("Recent", str(lessons)),
                _row("Open fixes", str(sum(_object(fix).get("state") == "open" for fix in fixes.values()))),
                *[_row(label, str(counts[kind])) for kind, label in LESSON_KINDS.items()],
            ]},
            {"title": "Today's runs", "rows": [
                _row("Work units", str(len(today))),
                _row("Failures", str(failures), "bad" if failures else None),
            ]},
        ],
    }


def output_path(cfg, out=None, *, manual=False):
    environment = os.environ.get("SDLC_TODO_FEED")
    configured = _object(cfg.get("board_feed")).get("out")
    value = out if out is not None else environment or configured or (DEFAULT_OUT if manual else None)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid board feed output")
    path = Path(value).expanduser()
    if out is None and not environment and configured and not path.is_absolute():
        return cfg.path.parent / path
    return path


def publish(cfg, out, *, guard_failed=False):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    directory = os.open(out.parent, os.O_RDONLY)
    try:
        # Serialize read and replace so an earlier tick cannot overwrite a newer snapshot.
        # The state writer uses a 0600 temporary file and atomic replace.
        fcntl.flock(directory, fcntl.LOCK_EX)
        write_json(out, snapshot(cfg, guard_failed=guard_failed))
    finally:
        os.close(directory)


def publish_if_enabled(cfg, *, guard_failed=False):
    try:
        out = output_path(cfg)
        if out is not None:
            publish(cfg, out, guard_failed=guard_failed)
    except Exception:  # Optional output must never interrupt factory execution.
        with suppress(OSError):
            print("factory-board-feed: snapshot write failed", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        publish(cfg, output_path(cfg, args.out, manual=True))
    except Exception:
        print("factory-board-feed: snapshot write failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
