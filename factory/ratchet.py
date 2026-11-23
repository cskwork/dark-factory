"""Record lower-is-better factory metrics and check them against their ceilings."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import subprocess
import sys

from .config import RATCHET_METRICS, load_config
from .drain import epoch
from .lessons import append_lesson, signature
from .state import locked, read_object, write_json
from .usage import unit_metric

FILENAME = "ratchets.json"
COMMAND_TIMEOUT = 10
HISTORY_LIMIT = 30
DAY_SECONDS = 86400


def _recent(stamp, now, days):
    try:
        return now - days * DAY_SECONDS <= epoch(stamp) <= now
    except (ValueError, TypeError, OverflowError, OSError):
        return False


def _events(path, now, days):
    try:
        stream = path.open(encoding="utf-8")
    except FileNotFoundError:
        return
    with stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f"{path.name}:{number}: expected an object")
                if _recent(row.get("at"), now, days):
                    yield row


def _human_decisions(directory, now):
    return sum(row.get("event") == "decision" and row.get("by") != "judge"
               for row in _events(directory / "decisions.jsonl", now, 7)) / 7


def _repeated_lessons(directory, now):
    counts = Counter(signature(row) for row in _events(directory / "lessons.jsonl", now, 14)
                     if isinstance(row.get("kind"), str)
                     and isinstance(row.get("what", row.get("observation")), str))
    return sum(count >= 3 for count in counts.values())


def _run_metrics(cfg, now):
    from .runtime import run_folder

    directory = cfg.state_dir
    current = read_object(directory / "running.json").get("token")
    runs = empty = units = missing = 0
    for path in (directory / "runs").glob("*/summary.json"):
        if path.parent.name == current or not run_folder(cfg, path.parent, set()):
            continue
        summary = read_object(path)
        started = summary.get("startedAt", summary.get("started_at"))
        recent_run = _recent(started, now, 7)
        rows = summary.get("units", summary.get("runtimeUnits"))
        if not isinstance(rows, list):
            if recent_run:
                raise ValueError("run summary has no units array")
            continue
        if recent_run:
            runs += 1
            empty += not rows
        for unit in rows:
            if not isinstance(unit, dict):
                raise ValueError("invalid unit in run summary")
            if _recent(unit.get("startedAt", started), now, 7):
                units += 1
                missing += all(unit_metric(unit, name) is None for name in ("tokens", "costUsd"))
    return {"no_progress_run_ratio": empty / runs if runs else None,
            "cost_unknown_ratio": missing / units if units else None}


def _measure(cfg, now):
    values, errors = {}, {}
    readers = [
        (("human_decisions_per_day",), lambda: {"human_decisions_per_day": _human_decisions(cfg.state_dir, now)}),
        (("repeat_lesson_signatures",), lambda: {"repeat_lesson_signatures": _repeated_lessons(cfg.state_dir, now)}),
        (("no_progress_run_ratio", "cost_unknown_ratio"), lambda: _run_metrics(cfg, now)),
    ]
    for names, read in readers:
        try:
            values.update(read())
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
            errors.update(dict.fromkeys(names, str(error)))
    for name, command in cfg.get("ratchet", {}).get("commands", {}).items():
        try:
            result = cfg.run(command, timeout=COMMAND_TIMEOUT)
            if result.returncode:
                raise ValueError(f"command exit {result.returncode}")
            value = float(result.stdout.strip())
            if not math.isfinite(value):
                raise ValueError("expected one finite number")
            values[name] = value
        except subprocess.TimeoutExpired:
            errors[name] = "command timeout"
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            errors[name] = str(error)
    return values, errors


def _evaluate(cfg, state, now, *, initialize, tighten):
    stamp = now.isoformat()
    values, errors = _measure(cfg, now.timestamp())
    ceiling, baseline = state.get("ceiling", {}), state.get("baseline", {})
    report = {"at": stamp, "day": now.date().isoformat(), "metrics": {}}
    names = (*RATCHET_METRICS, *cfg.get("ratchet", {}).get("commands", {}))
    for name in names:
        value = values.get(name)
        limit = ceiling.setdefault(name, None)
        baseline.setdefault(name, None)
        status = "ok"
        if name in errors:
            status = "error"
        elif value is None or limit is None and not (tighten or initialize):
            status = "unknown"
        elif limit is not None and value > limit:
            status = "regression"
        elif initialize or tighten and (limit is None or value < limit):
            ceiling[name] = value
            if baseline[name] is None:
                baseline[name] = value
        row = {"value": value, "ceiling": ceiling[name], "status": status}
        if name in errors:
            row["error"] = errors[name]
        report["metrics"][name] = row
    statuses = {row["status"] for row in report["metrics"].values()}
    report["status"] = next(status for status in ("regression", "error", "unknown", "ok") if status in statuses)
    state.update(ceiling=ceiling, baseline=baseline, setAt=state.get("setAt", stamp),
                 updatedAt=stamp, last=report, history=[*state.get("history", []), report][-HISTORY_LIMIT:])
    return report


def _run(cfg, *, now, initialize=False, tighten=False, automatic=False):
    if cfg.get("ratchet") is None:
        if automatic:
            return None
        raise ValueError("ratchet is disabled; add a [ratchet] section")
    now = datetime.now().astimezone() if now is None else now
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    path = cfg.state_dir / FILENAME
    with locked(cfg.state_dir / ".ratchet.lock"):
        state = read_object(path)
        if initialize and state:
            raise ValueError("ratchets already initialized")
        if not state and not (initialize or automatic):
            raise ValueError("run factory-ratchet init first")
        if automatic and state.get("last", {}).get("day") == now.date().isoformat():
            return None
        report = _evaluate(cfg, state, now, initialize=initialize or not state, tighten=tighten)
        write_json(path, state)
        lesson_errors = {}
        for name, row in report["metrics"].items():
            if row["status"] != "regression":
                continue
            try:
                append_lesson(cfg, {"kind": "ratchet_regression", "what": f"Ratchet regression: {name}",
                                    "metric": name, "value": row["value"], "ceiling": row["ceiling"]}, now=now)
            except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
                lesson_errors[name] = str(error)
        if lesson_errors:
            report["lessonErrors"] = lesson_errors
            write_json(path, state)
        return report


def daily(cfg, now=None):
    """Initialize on the first eligible tick, then check and tighten once per local day."""
    return _run(cfg, now=now, tighten=True, automatic=True)


def _print(report):
    for name, row in report["metrics"].items():
        detail = row.get("error") or f"{row['value']} (ceiling {row['ceiling']})"
        print(f"{name}: {row['status']} — {detail}")
    for name, error in report.get("lessonErrors", {}).items():
        print(f"lesson {name}: {error}", file=sys.stderr)


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", parents=[common])
    check = commands.add_parser("check", parents=[common])
    check.add_argument("--tighten", action="store_true")
    check.add_argument("--json", action="store_true")
    commands.add_parser("status", parents=[common])
    args = parser.parse_args(argv)
    try:
        cfg = load_config(getattr(args, "config", None))
        if args.command == "status":
            last = read_object(cfg.state_dir / FILENAME).get("last")
            if cfg.get("ratchet") is None:
                print("ratchet: disabled")
            if last is not None:
                _print(last)
            elif cfg.get("ratchet") is not None:
                print("ratchet: uninitialized")
            return 0
        report = _run(cfg, now=None, initialize=args.command == "init", tighten=getattr(args, "tighten", False))
        if getattr(args, "json", False):
            print(json.dumps(report))
        else:
            _print(report)
        return {"regression": 10, "error": 2}.get(report["status"], 0)
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"factory-ratchet: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
