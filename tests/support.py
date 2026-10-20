"""Local fixtures; commands and launcher outputs never contact external systems."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from factory.config import Config

ROOT = Path(__file__).resolve().parents[1]


def event(identity="1", **changes):
    return {"key": "PROJ-123", "id": identity, "author": "reporter", "text": "Please investigate",
            "mentions_me": True, "assigned_to_me": True, "answered_by_me_after": False, **changes}


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="factory fixture ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir()
        self.stub = self.root / "stub.py"
        self.stub.write_text('''import json, pathlib, sys
root = pathlib.Path(__file__).parent
kind = sys.argv[1]
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
settings = json.loads((root / "responses.json").read_text())
value = settings.get(kind, {})
print(value.get("stdout", ""))
print(value.get("stderr", ""), file=sys.stderr, end="")
sys.exit(value.get("code", 0))
''')
        self.responses = {"scan": {"stdout": json.dumps({"events": [], "errors": []})},
                          "launch": {"stdout": "run-123"}}
        self.save_responses()
        role = {"provider": "provider-one", "model": "model-small", "thinking": "low", "mode": "read-only"}
        self.data = {"project_root": str(self.root), "state_dir": "state", "duplicate_window_seconds": 1500,
                     "lock_stale_seconds": 10800, "key_pattern": r"[A-Z]+-\d+",
                     "intake": {"scan_cmd": [sys.executable, str(self.stub), "scan"]},
                     "launcher": {"command": [sys.executable, str(self.stub), "launch", "--provider", "{provider}",
                                                "--model", "{model}", "--thinking", "{thinking}", "--mode", "{mode}", "--cwd", "{cwd}"]},
                     "roles": {"waker": role.copy(), "orchestrator": {**role, "model": "model-large", "thinking": "high"},
                               "workers": {"builder": role.copy(), "reviewer": {**role, "model": "model-review"}}},
                     "triage": {"mode": "shadow", "confidence_threshold": 0.9}}
        self.config = self.root / "factory.toml"
        self.cfg = Config(self.data, self.config)

    def save_responses(self):
        (self.root / "responses.json").write_text(json.dumps(self.responses))

    def scan(self, events=None, errors=None, **response):
        self.responses["scan"] = {"stdout": json.dumps({"events": events or [], "errors": errors or []}), **response}
        self.save_responses()

    def write(self, name, value):
        path = self.state / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def read(self, name):
        return json.loads((self.state / name).read_text())

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def save_config(self):
        lines = []
        def table(value, prefix=""):
            scalars = {k: v for k, v in value.items() if not isinstance(v, dict)}
            if prefix:
                lines.append("[" + prefix + "]")
            for key, item in scalars.items():
                lines.append(key + " = " + json.dumps(item))
            for key, item in value.items():
                if isinstance(item, dict):
                    table(item, prefix + "." + key if prefix else key)
        table(self.data)
        self.config.write_text("\n".join(lines) + "\n")

    def cli(self, binary, *args):
        self.save_config()
        return subprocess.run([str(ROOT / "bin" / binary), "--config", str(self.config), *args],
                              capture_output=True, text=True, timeout=20, cwd=self.root,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
