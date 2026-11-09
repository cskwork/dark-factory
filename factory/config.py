"""One TOML configuration; commands are argv templates, never shell programs."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import tomllib


class StateDirectoryError(ValueError):
    """Unsafe state directory supplied by configuration."""


def strings(value):
    """A list of nonempty strings (an empty list is allowed)."""
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def argv(value):
    """A nonempty argv list or command string."""
    return bool(value) and (strings(value) or isinstance(value, str) and bool(value.strip()))


class Config:
    def __init__(self, data, path):
        self.data = data
        self.path = Path(path).expanduser().resolve()
        self.project_root = self.resolve(data.get("project_root", "."), self.path.parent)
        state_dir = data.get("state_dir", ".factory")
        if isinstance(state_dir, str) and not state_dir.strip():
            raise StateDirectoryError("invalid state_dir: must not be empty or whitespace")
        self.state_dir = self.resolve(state_dir, self.project_root)
        if self.state_dir == self.state_dir.parent:
            raise StateDirectoryError("invalid state_dir: must not resolve to the filesystem root")
        re.compile(self.get("key_pattern", r"[A-Z][A-Z0-9]+-\d+"))
        for name in ("duplicate_window_seconds", "lock_stale_seconds"):
            value = self.get(name, 1500 if name.startswith("duplicate") else 10800)
            if type(value) is not int or value < 0:
                raise ValueError("invalid " + name)
        for name in ("liveness", "drain"):
            settings = self.get(name, {})
            if not isinstance(settings, dict) or type(settings.get("enabled", False)) is not bool:
                raise ValueError("invalid " + name + " switch")
        triage = self.get("triage", {})
        if triage.get("mode", "shadow") not in ("shadow", "enforce"):
            raise ValueError("invalid triage mode")
        threshold = triage.get("confidence_threshold", 0.9)
        if type(threshold) not in (int, float) or not 0 <= threshold <= 1:
            raise ValueError("invalid confidence threshold")
        reconcile = self.get("reconcile", {})
        if (not isinstance(reconcile, dict) or not isinstance(reconcile.get("status_map", {}), dict)
                or not all(value in ("done", "awaiting-reporter") for value in reconcile.get("status_map", {}).values())
                or not strings(reconcile.get("done_stages", []))):
            raise ValueError("invalid reconcile settings")
        refusal = self.get("drain", {}).get("model_refusal")
        if refusal is not None:
            workers = self.get("roles", {}).get("workers", {})
            if (not isinstance(refusal, dict) or not refusal.get("patterns") or not strings(refusal["patterns"])
                    or refusal.get("retry_worker") not in workers):
                raise ValueError("invalid drain.model_refusal")
            for pattern in refusal["patterns"]:
                try:
                    re.compile(pattern)
                except re.error as error:
                    raise ValueError("invalid drain.model_refusal pattern") from error
        decisions = self.get("decisions", {})
        if not isinstance(decisions, dict) or decisions.get("surface", "html") not in ("app", "tui", "web", "html"):
            raise ValueError("invalid decisions.surface: use app, tui, web or html")
        cleanup = self.get("cleanup", {})
        if (not isinstance(cleanup, dict) or not strings(cleanup.get("roots", []))
                or not strings([cleanup.get("merge_branch_prefix", "merge/")])):
            raise ValueError("invalid cleanup settings")
        lessons = self.get("lessons", {})
        if not isinstance(lessons, dict):
            raise ValueError("invalid lessons settings")
        for name, default in (("escalate_count", 3), ("window_days", 14), ("cooldown_days", 7)):
            value = lessons.get(name, default)
            if type(value) is not int or value <= 0:
                raise ValueError("invalid lessons." + name)
        self._validate_judge(self.get("judge", {}))
        worker = self.get("worker", {})
        if not isinstance(worker, dict):
            raise ValueError("invalid worker settings")
        env = worker.get("env", {})
        if (not isinstance(env, dict) or any(
                not isinstance(name, str) or not name or "=" in name or "\0" in name
                or not isinstance(value, str) or "\0" in value for name, value in env.items())):
            raise ValueError("invalid worker.env")

    @staticmethod
    def _validate_judge(judge):
        """Optional [judge]; a missing section keeps the judge off."""
        if not isinstance(judge, dict):
            raise ValueError("invalid judge settings")
        if not judge:
            return
        if not argv(judge.get("command")):
            raise ValueError("invalid judge.command: an argv list or string is required")
        confidence = judge.get("min_confidence", 0.8)
        if (judge.get("mode", "off") not in ("off", "shadow", "on")
                or type(confidence) not in (int, float) or not 0 <= confidence <= 1
                or not strings(judge.get("human_only", []))
                or not strings([judge.get("setting_title", "[factory] judge")])):
            raise ValueError("invalid judge settings")
        for name, default in (("risk_per_key_per_day", 2), ("timeout_seconds", 900)):
            value = judge.get(name, default)
            if type(value) is not int or value <= 0:
                raise ValueError("invalid judge." + name)
        rule = judge.get("promotion")
        if rule is None:
            return
        if (not isinstance(rule, dict) or not strings([rule.get("qa_done_status")])
                or not strings(rule.get("stages", [])) or not strings([rule.get("nudge_template", "{reporter}")])
                or not argv(rule.get("facts_cmd")) or not argv(rule.get("comment_cmd"))):
            raise ValueError("invalid judge.promotion")
        try:
            rule.get("nudge_template", "{reporter}").format_map({"reporter": "", "status": ""})
        except (KeyError, ValueError, IndexError) as error:
            raise ValueError("invalid judge.promotion.nudge_template: use {reporter} and {status}") from error

    @staticmethod
    def resolve(value, parent):
        path = Path(value).expanduser()
        return (parent / path).resolve() if not path.is_absolute() else path.resolve()

    def get(self, key, default=None):
        return self.data.get(key, default)

    def valid_key(self, value):
        return isinstance(value, str) and re.fullmatch(self.get("key_pattern", r"[A-Z][A-Z0-9]+-\d+"), value) is not None

    def command(self, template, **values):
        if not isinstance(template, (str, list)):
            raise ValueError("command must be a string or argv list")
        parts = shlex.split(template) if isinstance(template, str) else list(template)
        if not parts or not all(isinstance(part, str) for part in parts):
            raise ValueError("empty or invalid command")
        context = {"cwd": str(self.project_root), "project_root": str(self.project_root),
                   "state_dir": str(self.state_dir), "config": str(self.path), **values}
        # Split BEFORE formatting: spaces and shell metacharacters stay one argument.
        return [part.format_map(context) for part in parts]

    def run(self, template, *, timeout=None, **values):
        # A caller may shorten a single command; it can never exceed the configured cap.
        limit = self.get("command_timeout_seconds", 300)
        return subprocess.run(self.command(template, **values), cwd=self.project_root,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=limit if timeout is None else min(timeout, limit))


def load_config(path=None):
    path = Path(path or os.environ.get("FACTORY_CONFIG", "factory.toml")).expanduser().resolve()
    with path.open("rb") as stream:
        return Config(tomllib.load(stream), path)
