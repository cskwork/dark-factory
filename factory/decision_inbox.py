"""Human decisions and alerts on the sdlc-kit to-do list instead of a browser page.

`[decisions] surface` picks the one place a human decides: `html` (default) is the
local decision page with one-time-token buttons (decision_page). `app`, `tui` and
`web` are views of the same sdlc-kit to-do list (`~/.sdlc/todo.md`; `SDLC_TODO`
overrides the file, `SDLC_KIT` the kit, default `~/.agents/skills/sdlc-kit`).

With a to-do surface there is no page and no server. A decision becomes an item
titled `[decision D<n>] KEY · title` that waits for the human ("Needs you"). The
human's reply on that item is the decision, for every kind:

- approval / sql-run: `approve` (also `1`, `승인`) runs the approved action;
- choice: a number (`2`, `2번`, `D12 2`), an option id or its label;
- `hold` (also `보류`) records nothing and closes the item.

Any other reply re-asks on the same item. The watcher (`watch`) polls every
POLL_SECONDS while an item is open; decision-request and every factory-wake tick
start it, and a lock keeps one per state directory. A reply is recorded through
decision_page.decide, so the single-use claim, the audit line in decisions.jsonl,
the action and the focused wake are the same as a button click. The decision is
recorded before the item is closed; a crash in between is finished next cycle and
a repeated reply hits the used claim, so nothing is recorded twice.

Alerts sent through notify.cmd also update one item `[factory alert] <project>`
whose waiting text is the latest alert.

The factory never writes `human` events: it only runs add, note, wait, done, list.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from . import decision_page as dp
from .config import load_config
from .state import locked, read_object, write_json

SURFACES = ("app", "tui", "web", "html")
TITLE = "[decision D{n}] "
DECISION_ITEM = re.compile(r"^\[decision D\d+\] ")
ALERT_TITLE = "[factory alert] "
APPROVE = {"approve", "approved", "yes", "ok", "1", "1번", "승인"}
HOLD = {"hold", "보류"}
POLL_SECONDS = 60
BY = "todo"
ROOT = Path(__file__).resolve().parents[1]


def surface(cfg):
    """The configured decision surface; Config already rejected unknown values."""
    return cfg.get("decisions", {}).get("surface", "html")


def kit():
    return Path(os.environ.get("SDLC_KIT") or Path.home() / ".agents/skills/sdlc-kit").expanduser()


def todo_cli(args, timeout=60):
    """Run the sdlc-kit to-do CLI; list --json is parsed, other commands return stdout."""
    result = subprocess.run(["node", str(kit() / "todo/todo.mjs"), *args], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:300] or f"to-do exit {result.returncode}")
    out = result.stdout.strip()
    if args[:1] == ["list"] and "--json" in args:
        return json.loads(out) if out else []
    return out


def line(text, limit=900):
    return " ".join(str(text or "").split())[:limit]


def _registry_path(cfg):
    return cfg.state_dir / "decision-pages" / "todo.json"


@contextmanager
def _registry(cfg):
    """todo.json maps decision_id -> {code, key, todoId, state, itemDone}; entries are never removed."""
    path = _registry_path(cfg)
    with locked(path.with_name(".todo.lock")):
        registry = read_object(path)
        yield registry
        write_json(path, registry)


def _update(cfg, decision_id, **fields):
    with _registry(cfg) as registry:
        registry[decision_id].update(fields, updatedAt=datetime.now(timezone.utc).isoformat())


def _recommended(spec, option):
    return " (recommended)" if spec.get("recommended") == option["id"] else ""


def prompt(spec, code):
    """The one waiting line the human sees, including how to answer."""
    head = f"{code} {spec['key']} · {spec.get('question') or spec['title']}"
    if spec["kind"] == "choice":
        options = " · ".join(f"{n}) {o['label']}{_recommended(spec, o)}" for n, o in enumerate(spec["options"], 1))
        return line(f"{head} — {options} — reply with a number, or 'hold'")
    action = ("approving runs the configured approval action" if spec["kind"] == "approval"
              else f"approving runs the reviewed SQL on {spec['config']}")
    return line(f"{head} — {action} — reply 'approve' or 'hold'")


def details(spec):
    """The evidence a decision page shows, one note line each."""
    rows = [f"Summary: {spec['summary']}"] if spec.get("summary") else []
    if spec["kind"] == "choice":
        rows += [f"{n}) {o['label']}: {o['detail']}" for n, o in enumerate(spec["options"], 1) if o.get("detail")]
    elif spec["kind"] == "sql-run":
        rows += [f"Connection: {spec['config']}; host prefix: {spec['expect_host']}; SHA-256: {spec['sql_sha256']}",
                 f"Guard: {spec['guard_sql']} = {spec['guard_expect']}",
                 f"SQL ({spec['file']}): {spec['sql_text']}"]
    else:
        rows.append(f"Approve the configured action for {spec['key']}.")
    return [line(row) for row in rows if line(row)]


def open_item(cfg, spec, *, todo=None):
    """Turn a created request into a waiting to-do item. Returns (code, item id)."""
    todo = todo or todo_cli
    with _registry(cfg) as registry:
        code = f"D{len(registry) + 1}"
        registry[spec["decision_id"]] = {"code": code, "key": spec["key"], "todoId": None, "state": "opening",
                                         "itemDone": False, "createdAt": datetime.now(timezone.utc).isoformat()}
    try:
        title = line(TITLE.format(n=code[1:]) + f"{spec['key']} · {spec['title']}", 200)
        item = todo(["add", title, "--project", str(cfg.project_root), "--by", "factory"])
        for row in details(spec):
            todo(["note", item, row])
        todo(["wait", item, prompt(spec, code)])
    except Exception:
        _update(cfg, spec["decision_id"], state="failed", itemDone=True, reason="todo-open-failed")
        raise
    _update(cfg, spec["decision_id"], todoId=item, state="open")
    return code, item


def option_for(spec, text):
    """A reply -> option id, None for hold, or ValueError explaining what to answer."""
    words = re.sub(r"^[Dd]\d+(?:\s*[.:-]\s*|\s+)", "", line(text).strip(" ."))
    lower = words.lower()
    if lower in HOLD:
        return None
    if spec["kind"] != "choice":
        if lower in APPROVE:
            return "approve"
        raise ValueError("reply 'approve' or 'hold'")
    options = spec["options"]
    number = re.fullmatch(r"(\d+)\s*번?", words)
    if number and 1 <= int(number[1]) <= len(options):
        return options[int(number[1]) - 1]["id"]
    for option in options:
        if lower in (option["id"].lower(), option["label"].lower()):
            return option["id"]
    raise ValueError(f"reply with a number from 1 to {len(options)}, an option id or label, or 'hold'")


def reply_id(item, reply):
    digest = hashlib.sha1(f"{reply.get('at')}\0{reply.get('what')}".encode()).hexdigest()[:12]
    return f"todo:{item}:{digest}"


def outcome(spec, option, result):
    """One closing line for the item, from decision_page.decide's result."""
    if result["status"] == "used":
        return "Already decided; nothing was recorded again."
    if result["status"] == "failed":
        return line("Action failed; nothing continues until the orchestrator reports it: "
                    + str(result.get("error") or json.dumps(result.get("detail"), default=str)), 300)
    if spec["kind"] == "choice":
        label = next(o["label"] for o in spec["options"] if o["id"] == option)
        return f"Recorded '{label}'; the line resumes."
    return "Approved and ran; the line resumes."


def poll(cfg, *, todo=None, actions=None, wake=None):
    """Handle new replies on open decision items. Returns [(code, status)]."""
    todo = todo or todo_cli
    pending = {decision_id: entry for decision_id, entry in read_object(_registry_path(cfg)).items()
               if entry.get("todoId") and not entry.get("itemDone")}
    if not pending:
        return []
    rows = {row.get("id"): row for row in todo(["list", "--json"]) or []}
    handled = []
    for decision_id, entry in pending.items():
        code, item = entry["code"], entry["todoId"]
        row = rows.get(item)
        if row is None or row.get("status") == "done":
            # The human closed or removed the item without replying: like closing a page, nothing is recorded.
            if entry["state"] == "open":
                _update(cfg, decision_id, state="closed", reason="item-closed")
            _update(cfg, decision_id, itemDone=True)
            handled.append((code, "closed"))
            continue
        if entry["state"] != "open":
            # Recorded before a crash; finish closing the item.
            todo(["done", item, "This decision is already closed."])
            _update(cfg, decision_id, itemDone=True)
            handled.append((code, "closed"))
            continue
        replies = [r for r in row.get("replies") or [] if r.get("who") == "human"]
        if not replies:
            continue
        reply = replies[-1]
        text = re.sub(r"^reply — ", "", reply.get("what") or "")
        spec = json.loads((cfg.state_dir / "decisions" / (decision_id + ".request.json")).read_text())
        try:
            option = option_for(spec, text)
        except ValueError as error:
            todo(["wait", item, line(f"Could not use '{text}': {error}. {prompt(spec, code)}")])
            handled.append((code, "invalid"))
            continue
        if option is None:
            _update(cfg, decision_id, state="held")
            todo(["done", item, "Held; nothing was recorded."])
            _update(cfg, decision_id, itemDone=True)
            handled.append((code, "held"))
            continue
        note = line(f"Human replied '{text}' on to-do item {item} at {dp._stamp()}", 300)
        result = dp.decide(cfg, spec, option, actions=actions, wake=wake, by=BY, note=note,
                           message_id=reply_id(item, reply))
        _update(cfg, decision_id, state="decided", status=result["status"])
        todo(["done", item, outcome(spec, option, result)])
        _update(cfg, decision_id, itemDone=True)
        handled.append((code, {"used": "duplicate", "done": "decided"}.get(result["status"], result["status"])))
    return handled


def alert(cfg, message, *, todo=None):
    """Put the latest alert on one '[factory alert] <project>' item. html: do nothing."""
    if surface(cfg) == "html":
        return False
    todo = todo or todo_cli
    title = ALERT_TITLE + cfg.project_root.name
    rows = todo(["list", "--json"]) or []
    item = next((r["id"] for r in rows if r.get("title") == title and r.get("status") != "done"), None)
    item = item or todo(["add", title, "--by", "factory"])
    todo(["wait", item, line(message)])
    return True


def open_items(cfg):
    return any(entry.get("todoId") and not entry.get("itemDone")
               for entry in read_object(_registry_path(cfg)).values())


def start_watcher(cfg):
    """Start a detached watcher when a to-do decision is open; a running one keeps its lock and the new one exits."""
    if surface(cfg) == "html" or not open_items(cfg):
        return False
    pages = cfg.state_dir / "decision-pages"
    pages.mkdir(parents=True, exist_ok=True)
    with (pages / "todo-watch.log").open("a") as log:
        subprocess.Popen([sys.executable, "-m", "factory.decision_inbox", "--config", str(cfg.path), "watch"],
                         cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    return True


def watch(cfg, *, sleep=time.sleep, poller=None):
    """Poll while any decision item is open; one watcher per state directory."""
    poller = poller or poll
    pages = cfg.state_dir / "decision-pages"
    pages.mkdir(parents=True, exist_ok=True)
    with (pages / ".todo-watch.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        while open_items(cfg):
            try:
                for code, status in poller(cfg):
                    print(f"{datetime.now(timezone.utc).isoformat()} {code} {status}", flush=True)
            except Exception as error:  # One failed cycle (e.g. the to-do CLI) must not end the watcher.
                print(f"todo poll failed: {type(error).__name__}: {line(error, 300)}", flush=True)
            sleep(POLL_SECONDS)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Human decisions on the sdlc-kit to-do list")
    parser.add_argument("--config")
    parser.add_argument("command", choices=["surface", "watch", "poll", "alert"])
    parser.add_argument("--text", help="alert text")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if args.command == "surface":
        print(surface(cfg))
    elif args.command == "watch":
        return watch(cfg)
    elif args.command == "poll":
        for code, status in poll(cfg):
            print(code, status)
    else:
        if not args.text:
            parser.error("alert needs --text")
        alert(cfg, args.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
