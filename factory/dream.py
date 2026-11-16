"""Optional daily memory dreaming: after an idle tick, launch one agent with a consolidation brief.

Configured by [dream] command_check and command_brief (argv lists or shlex strings, no shell).
The check exits 10 when a dream is due and 0 when it is not; the brief prints the prompt. The
sdlc-kit's `sdlc.sh dream --check` and `sdlc.sh dream` follow this contract.

Order (the first match ends the tick):
  1. commands unset -> unset; state_dir/dream first word `off` -> off (nothing recorded).
  2. dream-last.json already has today's date -> already-today (nothing recorded).
     From here on the day is recorded in dream-last.json, so the check runs at most once a day.
  3. The last launched dreamer still running (launcher.inspect_cmd) -> skipped.
  4. Check exit 0 -> not-due; exit 10 -> due; anything else -> error.
  5. Due: run the brief and launch one agent with it through the configured launcher
     ([dream] role's model, default the orchestrator's; labelled role=dreamer, [launch] env applied).
A dream never changes the tick's outcome: every failure is recorded and swallowed.
"""
from datetime import datetime
import json
import os
import subprocess

from .config import switched_off
from .state import locked, read_object, write_json

DUE = 10
CHECK_TIMEOUT = 30
BRIEF_TIMEOUT = 60


def _last(cfg):
    try:
        return read_object(cfg.state_dir / "dream-last.json")
    except (OSError, ValueError):
        return {}


def _running(cfg, identity):
    """True when the previous dreamer is still running; False when unknown or no inspect adapter."""
    from .runtime import _adapter, _field
    settings = cfg.get("launcher", {})
    if not identity or not settings.get("inspect_cmd"):
        return False
    owner = json.loads(_adapter(cfg, "inspect", id=identity))
    fields = settings.get("fields", {})
    status = _field(owner, fields.get("inspect_status", fields.get("status", "Status")))
    return settings.get("status_map", {}).get(status, status) == "running"


def launch(cfg, brief):
    from .runtime import launch_id, launcher
    role = cfg.get("dream", {}).get("role", "orchestrator")
    env = cfg.launch_env()
    command = (launcher(cfg, role, label="dreamer") + cfg.env_flags(env)
               + ["--no-wait", "--title", "Factory dreamer", "-q", brief])
    response = subprocess.run(command, cwd=cfg.project_root, stdin=subprocess.DEVNULL,
                              env={**os.environ, **env}, capture_output=True, text=True,
                              timeout=cfg.get("launch_timeout_seconds", 60))
    if response.returncode or not response.stdout.strip():
        raise ValueError("launch failed")
    return launch_id(cfg, response.stdout)


def tick(cfg, now=None):
    """(outcome, record). A None record leaves dream-last.json untouched."""
    settings = cfg.get("dream", {})
    if not settings.get("command_check") or not settings.get("command_brief"):
        return "unset", None
    if switched_off(cfg.state_dir / "dream"):
        return "off", None
    now = datetime.now().astimezone() if now is None else now
    today = now.date().isoformat()
    if _last(cfg).get("day") == today:
        return "already-today", None
    with locked(cfg.state_dir / ".dream.lock"):
        last = _last(cfg)
        if last.get("day") == today:  # Another tick finished while this one waited.
            return "already-today", None
        outcome, record = _decide(cfg, last, {"at": now.isoformat(), "day": today})
        try:
            write_json(cfg.state_dir / "dream-last.json", record)
        except OSError:
            pass
        return outcome, record


def _decide(cfg, last, record):
    settings = cfg.get("dream", {})
    try:
        if _running(cfg, last.get("agentId")):
            return "skipped", {**record, "outcome": "skipped", "reason": "dreamer-running",
                               "agentId": last["agentId"]}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        return "skipped", {**record, "outcome": "skipped", "reason": "query-failed: " + type(error).__name__}
    try:
        check = cfg.run(settings["command_check"], timeout=CHECK_TIMEOUT)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return "error", {**record, "outcome": "error", "reason": "check: " + type(error).__name__}
    if check.returncode == 0:
        return "not-due", {**record, "outcome": "not-due"}
    if check.returncode != DUE:
        return "error", {**record, "outcome": "error", "reason": f"check exit {check.returncode}"}
    try:
        brief = cfg.run(settings["command_brief"], timeout=BRIEF_TIMEOUT)
        if brief.returncode or not brief.stdout.strip():
            return "error", {**record, "outcome": "error", "reason": f"brief exit {brief.returncode}"}
        identity = launch(cfg, brief.stdout)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        return "error", {**record, "outcome": "error", "reason": "launch: " + type(error).__name__}
    return "launched", {**record, "outcome": "launched", "agentId": identity}
