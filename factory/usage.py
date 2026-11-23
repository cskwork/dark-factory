"""Best-effort inference usage from Paseo and Claude Code receipts."""
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from .state import locked, read_object, write_json

PROBE_TIMEOUT = 3
PROBES_PER_TICK = 3
ORCHESTRATOR_RETRY_SECONDS = 24 * 60 * 60
ORCHESTRATOR_RECEIPT = "orchestrator-usage.json"


def metric_value(name, value):
    if name == "tokens":
        return value if type(value) is int and value >= 0 else None
    try:
        return float(value) if type(value) in (int, float) and 0 <= value < math.inf else None
    except OverflowError:
        return None


def unit_metric(unit, name):
    observations = unit.get("observations")
    row = observations.get(name) if isinstance(observations, dict) else None
    value = metric_value(name, row.get("value")) if isinstance(row, dict) else None
    usage = unit.get("usage")
    if value is None and isinstance(usage, dict):
        value = metric_value(name, usage.get(name))
    return value


def _counts(data, fields):
    return {name: metric_value("tokens", data.get(field)) for name, field in fields.items()}


def judge_usage(envelope):
    counts = envelope.get("usage")
    counts = _counts(counts if isinstance(counts, dict) else {}, {
        "inputTokens": "input_tokens", "outputTokens": "output_tokens",
        "cachedTokens": "cache_read_input_tokens", "cacheCreationTokens": "cache_creation_input_tokens",
    })
    cost = metric_value("costUsd", envelope.get("total_cost_usd"))
    if cost is None and all(value is None for value in counts.values()):
        return None
    tokens = None
    if counts["inputTokens"] is not None and counts["outputTokens"] is not None:
        # Claude reports the two cache input categories apart from ordinary input.
        tokens = sum(value or 0 for value in counts.values())
    return {"source": "claude-envelope", **counts, "tokens": tokens, "costUsd": cost}


def _flags(command, name):
    for index, part in enumerate(command):
        if part == name and index + 1 < len(command):
            yield command[index + 1]
        elif part.startswith(name + "="):
            yield part.split("=", 1)[1]


def paseo_target(command, *, attempt=None, token=None):
    """Return the lookup for a launch, adding a label only if its labels are ambiguous."""
    if Path(command[0]).name.lower() not in ("paseo", "paseo.exe"):
        return None
    labels = dict(label.split("=", 1) for label in _flags(command, "--label") if "=" in label)
    if attempt and labels.get("attempt") == attempt:
        selectors = {"attempt": attempt}
    elif not attempt and token and labels.get("run") == token and labels.get("role") == "orchestrator":
        selectors = {"run": token, "role": "orchestrator"}
    else:
        label = f"factory-usage={attempt or token}"
        command.extend(["--label", label])
        selectors = {"factory-usage": attempt or token}
    connection = [arg for name in ("--host", "--home") for value in _flags(command, name)
                  for arg in (name, value)]
    return {"executable": command[0], "selectors": selectors, "connection": connection}


def _probe(cfg, target, args, runner, env):
    command = [target["executable"], *args, *target["connection"]]
    result = runner(command, cwd=cfg.project_root, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, env=env,
                    timeout=min(PROBE_TIMEOUT, cfg.get("command_timeout_seconds", 300)))
    if result.returncode:
        raise ValueError(f"{args[0]} exit {result.returncode}")
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise ValueError(f"{args[0]} invalid JSON") from error


def paseo_usage(cfg, target, provider, *, runner=None, env=None):
    """Look up exactly one agent; return (usage, error). Never assume missing usage is zero."""
    if target is None:
        return None, "non-paseo-launcher"
    runner = subprocess.run if runner is None else runner
    try:
        filters = [arg for name, value in target["selectors"].items() for arg in ("--label", f"{name}={value}")]
        rows = _probe(cfg, target, ["ls", "-a", "--global", "--json", *filters], runner, env)
        if isinstance(rows, dict):
            rows = rows.get("agents", rows.get("Agents"))
        if not isinstance(rows, list):
            return None, "invalid-agent-list"
        if len(rows) != 1:
            return None, "agent-not-found" if not rows else "ambiguous-agent-label"
        row = rows[0]
        identity = row.get("id", row.get("Id")) if isinstance(row, dict) else None
        if not isinstance(identity, str) or not identity or identity.startswith("-"):
            return None, "invalid-agent-id"
        state = str(row.get("status", row.get("Status", ""))).lower()
        if state in ("running", "starting", "initializing", "queued", "pending"):
            return None, "agent-running"
        result = _probe(cfg, target, ["agent", "inspect", "--json", identity], runner, env)
        if not isinstance(result, dict):
            return None, "invalid-agent-inspection"
        if str(result.get("Status", result.get("status", ""))).lower() == "running":
            return None, "agent-running"
        last = result.get("LastUsage")
        if not isinstance(last, dict):
            return None, "missing-last-usage"
        counts = _counts(last, {"inputTokens": "InputTokens", "outputTokens": "OutputTokens",
                                "cachedTokens": "CachedTokens"})
        if counts["inputTokens"] is None or counts["outputTokens"] is None:
            return None, "invalid-last-usage"
        tokens = counts["inputTokens"] + counts["outputTokens"]
        cost = metric_value("costUsd", last.get("CostUsd"))
        if not tokens and not counts["cachedTokens"] and not cost:
            return None, "empty-last-usage"
        if provider.lower() == "codex" and cost == 0 and (tokens or counts["cachedTokens"]):
            cost = None
        return {"source": "paseo", **counts, "tokens": tokens, "costUsd": cost}, None
    except subprocess.TimeoutExpired:
        return None, "paseo-usage-timeout"
    except (OSError, subprocess.SubprocessError, TypeError, KeyError, RecursionError) as error:
        return None, "paseo-usage-" + type(error).__name__
    except ValueError as error:
        return None, "paseo-usage: " + str(error)


def register_orchestrator(cfg, token, started, target, provider):
    record = {"startedAt": started, "usage": None,
              "usageError": "awaiting-agent-end" if target else "non-paseo-launcher"}
    write_json(cfg.state_dir / "runs" / token / ORCHESTRATOR_RECEIPT, record)
    if target:
        write_json(cfg.state_dir / "pending-orchestrator-usage" / (token + ".json"),
                   {**record, "target": target, "provider": provider})


def _collect_orchestrator(cfg, path, current, now, runner, env):
    from .drain import epoch
    from .runtime import launcher

    receipt = cfg.state_dir / "runs" / path.stem / ORCHESTRATOR_RECEIPT
    started_at = None
    try:
        data = read_object(path)
        started = epoch(data["startedAt"])
        started_at = data["startedAt"]
        saved_target, provider = data["target"], data["provider"]
        if (not isinstance(saved_target, dict) or not isinstance(saved_target.get("executable"), str)
                or not isinstance(saved_target.get("selectors"), dict)
                or not isinstance(saved_target.get("connection"), list)
                or not isinstance(provider, str) or not provider):
            raise ValueError("invalid pending usage target or provider")
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as failure:
        quarantine = path.with_suffix(".json.corrupt")
        path.rename(quarantine)
        error = f"corrupt-pending-usage: {failure}"
        print(f"{quarantine.name}: {error}", file=sys.stderr)
        if started_at is None:
            try:
                started_at = read_object(receipt).get("startedAt")
            except (OSError, ValueError, RecursionError) as read_error:
                print(f"{receipt}: could not recover startedAt: {read_error}", file=sys.stderr)
        write_json(receipt, {"startedAt": started_at, "usage": None, "usageError": error})
        return

    if now - started >= ORCHESTRATOR_RETRY_SECONDS:
        previous = read_object(receipt)
        if previous.get("usage") is None:
            error = previous.get("usageError")
            if error is None or error == "awaiting-agent-end":
                error = "usage-probe-expired"
            write_json(receipt, {"startedAt": started_at, "usage": None, "usageError": error})
        path.unlink()
        return

    # Resolve the executable and connection from trusted configuration. The pending
    # receipt is evidence of which configuration was used when the agent launched.
    try:
        target = paseo_target(launcher(cfg, "orchestrator", token=path.stem), token=path.stem)
    except (ValueError, TypeError, KeyError) as failure:
        measured, error = None, "invalid-usage-target: " + type(failure).__name__
    else:
        if target != saved_target:
            measured, error = None, "usage-target-changed"
        else:
            measured, error = paseo_usage(cfg, target, provider, runner=runner, env=env)
    write_json(receipt, {"startedAt": started_at, "usage": measured, "usageError": error})
    if error and (error in ("agent-running", "empty-last-usage")
                  or error.startswith(("paseo-usage-", "paseo-usage: ls exit ", "paseo-usage: agent exit "))
                  or path.stem == current and error == "agent-not-found"):
        path.touch()  # Give other waiting receipts a turn.
    else:
        path.unlink()


def collect_orchestrators(cfg, *, runner=None, now=None):
    """A release runs inside the agent. Later wakes sample detached agents after the turn ends."""
    directory = cfg.state_dir / "pending-orchestrator-usage"
    if not directory.is_dir():
        return
    now = time.time() if now is None else now
    env = {**os.environ, **cfg.launch_env()}
    with locked(cfg.state_dir / ".orchestrator-usage.lock"):
        current = read_object(cfg.state_dir / "running.json").get("token")
        pending = []
        for path in directory.glob("*.json"):
            if not re.fullmatch(r"[0-9a-f]{32}", path.stem):
                continue
            try:
                pending.append((path.stat().st_mtime, path))
            except OSError as failure:
                print(f"{path.name}: usage tracking failed: {failure}", file=sys.stderr)
        for _, path in sorted(pending)[:PROBES_PER_TICK]:
            try:
                _collect_orchestrator(cfg, path, current, now, runner, env)
            except (OSError, ValueError, TypeError, RecursionError) as failure:
                print(f"{path.name}: usage tracking failed: {failure}", file=sys.stderr)
