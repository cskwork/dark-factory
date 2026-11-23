"""List factory switches and settings with their defaults and current values."""
import argparse
import json
import os
import sys

from .config import argv as is_argv, environment, load_config, strings
from .judge import MODES

MISSING = object()
INVALID_TABLE = "invalid: expected a TOML table"


def _enum(*choices):
    return lambda value: value if value in choices else "invalid: expected " + "|".join(choices)


def _bool(value):
    return value if type(value) is bool else "invalid: expected a boolean"


def _table(value):
    return bool(value) if isinstance(value, dict) else INVALID_TABLE


def _hook(value):
    return False if value == [] else True if is_argv(value) else "invalid: expected a command"


def _required_hook(value):
    return True if is_argv(value) else "invalid: expected a nonempty command"


def _list(value):
    return bool(value) if strings(value) else "invalid: expected a list of strings"


def _present(value):
    return True if isinstance(value, dict) else INVALID_TABLE


def _environment(value):
    return bool(value) if environment(value) else "invalid: expected a table of environment strings"


def _out(value):
    return value if isinstance(value, str) and value.strip() else "invalid: expected an output path"


CONFIG_OPTIONS = (
    ("liveness.enabled", False, "kill-switch", "Check launcher ownership before recovering a run", _bool),
    ("drain.enabled", False, "kill-switch", "Allow multiple sequential runtime units in one run", _bool),
    ("judge.mode", "off", "ramp", "Choose the judge mode before state and environment overrides", _enum(*MODES)),
    ("judge.command", False, "setting", "Enable the configured judge command", _required_hook),
    ("judge.promotion", False, "setting", "Use tracker facts for promotion approvals", _table),
    ("judge.extra_args", False, "setting", "Add configured judge flags when the minimal environment is on", _list),
    ("dream.command_check", False, "setting", "Check whether memory consolidation is due", _required_hook),
    ("dream.command_brief", False, "setting", "Build a memory consolidation brief", _required_hook),
    ("ratchet", False, "setting", "Check and tighten metrics daily", _present),
    ("drain.model_refusal", False, "setting", "Enable configured provider refusal handling", _table),
    ("ratchet.commands", False, "setting", "Collect additional metrics from local commands", _table),
    ("triage.mode", "shadow", "ramp", "Observe or enforce classification predictions", _enum("shadow", "enforce")),
    ("triage.classifier_cmd", False, "setting", "Enable the event classification adapter", _hook),
    ("decisions.surface", "html", "mode", "Choose where human decisions appear", _enum("html", "app", "tui", "web")),
    ("decisions.approval.on_approve", False, "setting", "Run the configured approval action", _hook),
    ("decisions.sql.connector_module", False, "setting", "Enable the configured SQL decision connector",
     lambda value: True if isinstance(value, str) and value.strip() else "invalid: expected a module name"),
    ("notify.cmd", False, "setting", "Send factory notifications through an adapter", _hook),
    ("liveness.alert_cmd", False, "setting", "Notify the operator about unconfirmed run owners", _hook),
    ("reconcile.done_stages", False, "setting", "Remove queue items at configured terminal stages", _list),
    ("reconcile.status_map", False, "setting", "Reconcile queue items with tracker statuses", _table),
    ("migrations", False, "setting", "Run the migration verifier before approval", _present),
    ("cleanup.roots", False, "setting", "Allow cleanup in the configured worktree roots", _list),
    ("launch.env", False, "setting", "Set configured environment values on agent launches", _environment),
    ("worker.env", False, "setting", "Set additional values on worker launches", _environment),
    ("launcher.inspect_cmd", False, "setting", "Inspect launcher status and archive state", _hook),
    ("launcher.archive_cmd", False, "setting", "Archive agents from completed runs", _hook),
    ("launcher.cancel_cmd", False, "setting", "Cancel an identified worker when it yields no result", _hook),
    ("lessons.classes", False, "setting", "Require a configured class on new CLI lessons", _list),
    ("models.forbidden", [], "setting", "Reject role models that match a configured pattern",
     lambda value: value if strings(value) else "invalid: expected a list of patterns"),
    ("board_feed.out", None, "setting", "Publish the board feed to this path", _out),
)


def _config_value(cfg, name, default, describe):
    value = cfg.data
    for part in name.split("."):
        if not isinstance(value, dict):
            return INVALID_TABLE
        value = value.get(part, MISSING)
        if value is MISSING:
            return default
    return describe(value)


def _row(name, path, default, current, kind, purpose):
    return {"name": name, "path": str(path), "default": default, "current": current,
            "kind": kind, "purpose": purpose, "retireBy": None}


def _file_value(path, default, choices, *, first_word=False):
    try:
        raw = path.read_text().strip()
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as error:
        return "invalid: " + type(error).__name__
    if first_word and raw:
        raw = raw.split()[0].lower()
    return _enum(*choices)(raw)


def status(cfg):
    configured = [_row(name, f"{cfg.path}#{name}", default, _config_value(cfg, name, default, describe), kind, purpose)
                  for name, default, kind, purpose, describe in CONFIG_OPTIONS]
    settings = {row["name"]: row["current"] for row in configured}
    switches = (
        ("min-env", "on", ("off", "on"), "kill-switch", "Apply the minimal launch environment", True, True),
        ("judge", settings["judge.mode"], MODES, "ramp", "Override the configured judge mode", False, bool(cfg.get("judge"))),
        ("dream", "on", ("off", "on"), "kill-switch", "Allow configured daily dreaming", True,
         settings["dream.command_check"] is True and settings["dream.command_brief"] is True),
        ("liveness", "on", ("off", "on"), "kill-switch", "Allow configured owner liveness checks", False, settings["liveness.enabled"]),
        ("drain", "1", ("0", "1"), "kill-switch", "Allow configured sequential draining", False, settings["drain.enabled"]),
    )
    rows = []
    for name, default, choices, kind, purpose, first_word, enabled in switches:
        path = cfg.state_dir / name
        current = _file_value(path, default, choices, first_word=first_word)
        if name == "judge" and (override := os.environ.get("FACTORY_JUDGE_MODE")) is not None and enabled:
            forced = _enum(*MODES)(override)
            if not str(current).startswith("invalid:"):
                current = forced
        if not str(current).startswith("invalid:") and not enabled:
            current = "0" if name == "drain" else "off"
        rows.append(_row(name, path, default, current, kind, purpose))
    for name, default, kind, purpose, describe in (
        ("FACTORY_JUDGE_MODE", None, "ramp", "Override the judge mode for a configured judge", _enum(*MODES)),
        ("SDLC_TODO_FEED", None, "setting", "Override the automatic board feed output path", _out),
    ):
        current = os.environ.get(name)
        rows.append(_row(name, f"env:{name}", default, describe(current) if current is not None else default, kind, purpose))
    return rows + configured


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("status", parents=[common])
    show.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        rows = status(load_config(getattr(args, "config", None), validate=False))
    except (OSError, ValueError, TypeError) as error:
        print(f"factory-flags: invalid: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(rows))
    else:
        for row in rows:
            retire = f", retire by {row['retireBy']}" if row["retireBy"] else ""
            print(f"{row['name']}: {row['current']} (default {row['default']}, {row['kind']}{retire})"
                  f" — {row['purpose']} [{row['path']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
