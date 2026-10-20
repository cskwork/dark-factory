"""One TOML configuration; commands are argv templates, never shell programs."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import tomllib


class Config:
    def __init__(self, data, path):
        self.data = data
        self.path = Path(path).expanduser().resolve()
        self.project_root = self.resolve(data.get("project_root", "."), self.path.parent)
        self.state_dir = self.resolve(data.get("state_dir", ".factory"), self.project_root)
        re.compile(self.get("key_pattern", r"[A-Z][A-Z0-9]+-\d+"))
        for name in ("duplicate_window_seconds", "lock_stale_seconds"):
            value = self.get(name, 1500 if name.startswith("duplicate") else 10800)
            if type(value) is not int or value < 0:
                raise ValueError("invalid " + name)
        triage = self.get("triage", {})
        if triage.get("mode", "shadow") not in ("shadow", "enforce"):
            raise ValueError("invalid triage mode")
        threshold = triage.get("confidence_threshold", 0.9)
        if type(threshold) not in (int, float) or not 0 <= threshold <= 1:
            raise ValueError("invalid confidence threshold")

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

    def run(self, template, **values):
        return subprocess.run(self.command(template, **values), cwd=self.project_root,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=self.get("command_timeout_seconds", 300))


def load_config(path=None):
    path = Path(path or os.environ.get("FACTORY_CONFIG", "factory.toml")).expanduser().resolve()
    with path.open("rb") as stream:
        return Config(tomllib.load(stream), path)
