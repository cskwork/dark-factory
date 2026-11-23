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
TIGHTEN_DAYS = 7
MIN_RATIO_DENOMINATOR = 5


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
                     and row["kind"] != "ratchet_regression"
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
    return {"no_progress_run_ratio": (empty, runs), "cost_unknown_ratio": (missing, units)}


def _measure(cfg, now, only=None):
    values, denominators, errors = {}, {}, {}
    readers = [
        (("human_decisions_per_day",), lambda: {"human_decisions_per_day": _human_decisions(cfg.state_dir, now)}),
        (("repeat_lesson_signatures",), lambda: {"repeat_lesson_signatures": _repeated_lessons(cfg.state_dir, now)}),
        (("no_progress_run_ratio", "cost_unknown_ratio"), lambda: _run_metrics(cfg, now)),
    ]
    for names, read in readers:
        if only is not None and only not in names:
            continue
        try:
            for name, measured in read().items():
                if isinstance(measured, tuple):
                    numerator, denominator = measured
                    denominators[name] = denominator
                    values[name] = numerator / denominator if denominator else None
                else:
                    values[name] = measured
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
            errors.update(dict.fromkeys(names, str(error)))
    for name, command in cfg.get("ratchet", {}).get("commands", {}).items():
        if only is not None and name != only:
            continue
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
    return values, denominators, errors


def _candidate(samples, name):
    rows = [sample.get(name, {}) for sample in samples]
    if len(rows) < TIGHTEN_DAYS or any(row.get("value") is None for row in rows):
        return None
    if any(row.get("denominator", MIN_RATIO_DENOMINATOR) < MIN_RATIO_DENOMINATOR for row in rows):
        return None
    return max(row["value"] for row in rows)


def _record(state, now, metrics):
    stamp = now.isoformat()
    statuses = {row["status"] for row in metrics.values()}
    status = next(status for status in ("regression", "error", "unknown", "ok") if status in statuses)
    report = {"at": stamp, "day": now.date().isoformat(), "metrics": metrics, "status": status}
    state.update(setAt=state.get("setAt", stamp), updatedAt=stamp,
                 last=report, history=[*state.get("history", []), report][-HISTORY_LIMIT:])
    return report


def _evaluate(cfg, state, now, names, *, initialize, tighten, automatic):
    values, denominators, errors = _measure(cfg, now.timestamp())
    ceiling, baseline = state.get("ceiling", {}), state.get("baseline", {})
    samples = state.get("dailyHistory", [])
    sample = {name: {"value": value} for name, value in values.items() if name not in errors}
    for name, denominator in denominators.items():
        if name in sample:
            sample[name]["denominator"] = denominator
    if automatic:
        samples = [*samples, sample][-TIGHTEN_DAYS:]
        state.update(lastDaily=now.date().isoformat(), dailyHistory=samples)
    metrics = {}
    for name in names:
        value = values.get(name)
        limit = ceiling.setdefault(name, None)
        baseline.setdefault(name, None)
        row = dict(sample.get(name, {"value": value}))
        if name in errors:
            status = "error"
            row["error"] = errors[name]
        elif value is None:
            status = "unknown"
        else:
            if limit is None and (initialize or tighten):
                if denominators.get(name, MIN_RATIO_DENOMINATOR) >= MIN_RATIO_DENOMINATOR:
                    ceiling[name] = baseline[name] = value
            elif tighten and limit is not None:
                candidate = _candidate(samples, name)
                if candidate is not None and value <= candidate and 0 <= candidate < limit:
                    ceiling[name] = candidate
            limit = ceiling[name]
            status = "unknown" if limit is None else "regression" if value > limit else "ok"
        metrics[name] = {**row, "ceiling": ceiling[name], "status": status}
    state.update(ceiling=ceiling, baseline=baseline)
    return _record(state, now, metrics)


def _regressed(state):
    if "regressed" in state:
        return set(state["regressed"])
    # Legacy reports lack episode state. An unknown or failed probe does not establish recovery.
    known = {}
    for report in [*state.get("history", []), state.get("last", {})]:
        known.update((name, row["status"]) for name, row in report.get("metrics", {}).items()
                     if row["status"] in ("regression", "ok"))
    return {name for name, status in known.items() if status == "regression"}


def _reset(cfg, state, now, name):
    values, denominators, errors = _measure(cfg, now.timestamp(), only=name)
    value = values.get(name)
    if value is None or name in errors:
        raise ValueError(f"cannot reset {name}: {errors.get(name, 'no measurement')}")
    state["baseline"][name] = state["ceiling"][name] = value
    state["regressed"] = sorted(_regressed(state) - {name})
    for sample in state.get("dailyHistory", []):
        sample.pop(name, None)
    row = {"value": value, "ceiling": value, "status": "ok"}
    if name in denominators:
        row["denominator"] = denominators[name]
    report = _record(state, now, {**state.get("last", {}).get("metrics", {}), name: row})
    return {**report, "metrics": {name: row}, "status": "ok"}


def _run(cfg, *, now, initialize=False, tighten=False, automatic=False, reset=None):
    if cfg.get("ratchet") is None:
        if automatic:
            return None
        raise ValueError("ratchet is disabled; add a [ratchet] section")
    names = (*RATCHET_METRICS, *cfg.get("ratchet", {}).get("commands", {}))
    if reset is not None and reset not in names:
        raise ValueError(f"unknown metric: {reset}")
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
        if automatic and state.get("lastDaily", "") >= now.date().isoformat():
            return None
        if reset is not None:
            report = _reset(cfg, state, now, reset)
            write_json(path, state)
            return report
        regressed = _regressed(state)
        report = _evaluate(cfg, state, now, names, initialize=initialize or not state,
                           tighten=tighten, automatic=automatic)
        regressions = {name for name, row in report["metrics"].items() if row["status"] == "regression"}
        recovered = {name for name, row in report["metrics"].items() if row["status"] == "ok"}
        state["regressed"] = sorted((regressed | regressions) - recovered)
        write_json(path, state)
        lesson_errors = {}
        for name in sorted(regressions - regressed):
            row = report["metrics"][name]
            try:
                append_lesson(cfg, {"kind": "ratchet_regression", "class": "other",
                                    "what": f"Ratchet regression: {name}",
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
    reset = commands.add_parser("reset", parents=[common])
    reset.add_argument("metric")
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
        report = _run(cfg, now=None, initialize=args.command == "init", tighten=getattr(args, "tighten", False),
                      reset=getattr(args, "metric", None))
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
