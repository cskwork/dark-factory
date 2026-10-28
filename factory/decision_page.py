"""Loopback human decisions with durable, single-use claims and wake recovery.

approvals.json is an object keyed by work key. Each value contains
decision_id, approved=true, approved_at, note and used_at=null. Consumers set used_at
after consumption while holding .state.lock; an approval is scoped to its
decision_id, never a blanket permission for later work.

decisions/<id>.wake-pending.json remains runnable until the orchestrator reads
the matching ledger/result and unlinks it under its .wake-pending.lock. Delivery
adds wake_delivered_at; retries skip delivered markers and never replay actions.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse
import webbrowser

from . import sql_runner
from .config import StateDirectoryError, load_config


class DecisionRefused(ValueError):
    """A safe, locally generated explanation without connector output."""


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _error(error):
    # Driver and subprocess exceptions can contain passwords or connection URLs.
    if isinstance(error, DecisionRefused):
        return str(error)
    return type(error).__name__ + ": operation failed; inspect the configured service securely"


@contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(data, handle, ensure_ascii=False, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _audit(cfg, item):
    with _lock(cfg.state_dir / "decisions.lock"):
        path = cfg.state_dir / "decisions.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            handle.write(json.dumps({"at": _stamp(), **item}, ensure_ascii=False, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def create_request(cfg, spec: dict) -> dict:
    """Freeze trusted request inputs; HTTP POST supplies only token and option."""
    spec = json.loads(json.dumps(spec))
    if spec.get("kind") not in {"approval", "sql-run", "choice"}:
        raise ValueError("unknown decision kind")
    if not cfg.valid_key(spec.get("key")):
        raise ValueError("decision key does not match configured key pattern")
    spec.setdefault("title", f"Decision for {spec['key']}")
    if spec["kind"] == "choice":
        options = spec.get("options", [])
        ids = [o.get("id") for o in options]
        if not ids or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("choice requires unique nonempty option ids")
        if any(not isinstance(o.get("label"), str) for o in options):
            raise ValueError("choice labels are required")
    if spec["kind"] == "sql-run":
        for field in ("config", "expect_host", "file", "guard_sql", "guard_expect"):
            if field not in spec or spec[field] is None:
                raise ValueError(f"sql-run requires {field}")
        source = Path(spec["file"])
        if not source.is_absolute():
            source = cfg.project_root / source
        snapshot = source.read_bytes()
        spec["file"] = str(source.resolve())
        spec["sql_text"] = snapshot.decode("utf-8")
        spec["sql_sha256"] = hashlib.sha256(snapshot).hexdigest()
        sql_runner._validate(spec["sql_text"])
        sql_runner._validate(spec["guard_sql"], guard=True)
    if spec["kind"] == "approval" and cfg.get("migrations") is not None:
        from .check_migrations import check
        report, code = check(cfg, repo=spec.get("migration_repo"),
                             base=spec.get("migration_base"), head=spec.get("migration_head"))
        if code != 0:
            raise DecisionRefused("migration verification failed; waiting=human-risk; no approval page")
        spec["migration_check"] = report
    spec["decision_id"] = secrets.token_hex(16)
    spec["token"] = secrets.token_urlsafe(32)
    spec["created_at"] = _stamp()
    directory = cfg.state_dir / "decisions"
    directory.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.chmod(0o700)
    directory.chmod(0o700)
    _write(directory / (spec["decision_id"] + ".request.json"), spec)
    return spec


def _approve(cfg, spec, note):
    command = cfg.get("decisions", {}).get("approval", {}).get("on_approve")
    if not command:
        raise DecisionRefused("approval on_approve command is not configured")
    with _lock(cfg.state_dir / ".state.lock"):
        path = cfg.state_dir / "approvals.json"
        approvals = json.loads(path.read_text()) if path.exists() else {}
        existing = approvals.get(spec["key"])
        if existing and existing.get("approved") and not existing.get("used_at"):
            raise DecisionRefused("an unused approval already exists for this key")
        if existing and existing.get("action_status") == "pending":
            raise DecisionRefused("an approval action is already pending for this key")
        approvals[spec["key"]] = {"decision_id": spec["decision_id"], "approved": False,
                                   "approved_at": _stamp(), "note": note, "used_at": None,
                                   "action_status": "pending"}
        _write(path, approvals)
    try:
        result = cfg.run(command, key=spec["key"], decision_id=spec["decision_id"], note=note)
        if result.returncode:
            raise DecisionRefused(f"approval command exited {result.returncode}")
    except Exception:
        with _lock(cfg.state_dir / ".state.lock"):
            approvals = json.loads(path.read_text())
            approval = approvals.get(spec["key"], {})
            if approval.get("decision_id") == spec["decision_id"]:
                approval.update(approved=False, action_status="failed")
                _write(path, approvals)
        raise
    with _lock(cfg.state_dir / ".state.lock"):
        approvals = json.loads(path.read_text())
        approval = approvals.get(spec["key"], {})
        if approval.get("decision_id") != spec["decision_id"]:
            raise DecisionRefused("approval changed while the command was running")
        approval.update(approved=True, action_status="done")
        _write(path, approvals)
    return {"ok": True, "detail": "approval command completed"}


def _sql(cfg, spec, note):
    del note
    content = Path(spec["file"]).read_bytes()
    if hashlib.sha256(content).hexdigest() != spec["sql_sha256"]:
        raise DecisionRefused("SQL file changed after the request was created")
    # Execute the reviewed snapshot, never reopen a potentially changing file.
    result = sql_runner.run(cfg, config_name=spec["config"], sql_text=spec["sql_text"],
                            expect_host=spec["expect_host"], guard_sql=spec["guard_sql"],
                            guard_expect=spec["guard_expect"])
    return {"ok": result["status"] == "done", "detail": result}


def _wake_command(cfg, key):
    return [str(Path(__file__).resolve().parents[1] / "bin" / "factory-wake"),
            "--config", str(cfg.path), "--now", "--key", key]


def _deliver_wake(cfg, key, wake):
    result = wake(key) if wake else subprocess.run(
        _wake_command(cfg, key), cwd=cfg.project_root, check=True,
        capture_output=True, text=True, timeout=30)
    if result is not None and hasattr(result, "returncode"):
        output = ((getattr(result, "stdout", "") or "") +
                  (getattr(result, "stderr", "") or "")).lower()
        if result.returncode or "skipping duplicate" in output or "already running" in output:
            raise DecisionRefused("wake was not delivered or was skipped")


def retry_pending_wakes(cfg, wake=None):
    """Retry only wake delivery, never replay a decision or its action."""
    outcomes = []
    for path in sorted((cfg.state_dir / "decisions").glob("*.wake-pending.json")):
        with _lock(path.with_suffix(".lock")):
            if not path.exists():
                continue
            item = json.loads(path.read_text())
            if item.get("wake_delivered_at"):
                continue
            try:
                _deliver_wake(cfg, item["key"], wake)
                _audit(cfg, {**item, "event": "wake", "ok": True, "recovered": True})
                _write(path, {**item, "wake_delivered_at": _stamp()})
                outcomes.append(True)
            except Exception as error:
                _audit(cfg, {**item, "event": "wake", "ok": False, "error": _error(error)})
                outcomes.append(False)
    return outcomes


def decide(cfg, spec, option, *, actions=None, wake=None):
    """Persist a one-time claim before any effect; concurrent callers lose."""
    options = {o["id"] for o in spec.get("options", [])} if spec["kind"] == "choice" else {"approve"}
    if option not in options:
        raise ValueError("unknown decision option")
    base = {"decision_id": spec["decision_id"], "key": spec["key"], "kind": spec["kind"]}
    claim = cfg.state_dir / "decisions" / (spec["decision_id"] + ".claimed")
    try:
        with os.fdopen(os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
            handle.write(_stamp() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        return {"status": "used", "ok": False}
    note = f"Human clicked decision {spec['decision_id']} at {_stamp()}"
    _audit(cfg, {**base, "event": "decision", "option": option, "by": "button", "note": note})
    pending = cfg.state_dir / "decisions" / (spec["decision_id"] + ".wake-pending.json")
    _write(pending, {**base, "wake_pending": True})
    try:
        if spec["kind"] == "choice":
            result = {"ok": True, "detail": option}
        else:
            action = (actions or {}).get(spec["kind"])
            result = action(spec, note) if action else (
                _approve(cfg, spec, note) if spec["kind"] == "approval" else _sql(cfg, spec, note))
            if isinstance(result, tuple):
                result = {"ok": result[0], "detail": result[1]}
    except Exception as error:
        result = {"ok": False, "error": _error(error)}
    result["status"] = "done" if result["ok"] else "failed"
    _audit(cfg, {**base, "event": "action", **result})
    _write(cfg.state_dir / "decisions" / (spec["decision_id"] + ".result.json"), result)
    # Wake even when the action failed so orchestration can report the failure.
    with _lock(pending.with_suffix(".lock")):
        try:
            _deliver_wake(cfg, spec["key"], wake)
            _audit(cfg, {**base, "event": "wake", "ok": True})
            _write(pending, {**base, "wake_pending": True, "wake_delivered_at": _stamp()})
            result["wake_ok"] = True
        except Exception as error:
            result.update(wake_ok=False, wake_error=_error(error))
            _audit(cfg, {**base, "event": "wake", "ok": False, "error": _error(error)})
    _write(cfg.state_dir / "decisions" / (spec["decision_id"] + ".result.json"), result)
    return result


def page(spec, message=None):
    esc = lambda value: html.escape(str(value), quote=True)
    content = f"<h1>{esc(spec['title'])}</h1><p>{esc(spec.get('summary', ''))}</p>"
    if spec["kind"] == "sql-run":
        content += (f"<p>Connection: {esc(spec['config'])}; host prefix: {esc(spec['expect_host'])}</p>"
                    f"<p>SHA-256: {esc(spec['sql_sha256'])}</p><pre>{esc(spec['sql_text'])}</pre>"
                    f"<p>Guard: {esc(spec['guard_sql'])} = {esc(spec['guard_expect'])}</p>")
    if spec["kind"] == "approval":
        content += "<p>Approve the configured action described above.</p>"
    if message:
        content += f"<pre>{esc(message)}</pre>"
    else:
        content += f"<p>{esc(spec.get('question', ''))}</p>"
        options = spec.get("options", []) if spec["kind"] == "choice" else [{"id": "approve", "label": "Approve"}]
        for option in options:
            content += ("<form method='post' action='/decide'>"
                        f"<input type='hidden' name='t' value='{esc(spec['token'])}'>"
                        f"<input type='hidden' name='option' value='{esc(option['id'])}'>"
                        f"<button type='submit'>{esc(option['label'])}</button>"
                        f"<p>{esc(option.get('detail', ''))}</p></form>")
        content += "<p>Close this page to defer. No decision is recorded.</p>"
    return ("<!doctype html><html lang='en'><meta charset='utf-8'>"
            "<meta name='referrer' content='no-referrer'><title>Human decision</title>"
            "<style>body{font:18px system-ui;max-width:850px;margin:3rem auto;padding:1rem}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eee;padding:1rem}"
            "button{font:inherit;padding:.8rem}form{margin:1rem 0}</style>" + content + "</html>")


def make_server(cfg, spec, *, actions=None, wake=None, port=0):
    if "decision_id" not in spec:
        spec = create_request(cfg, spec)
    state = {"used": False, "result": None}
    token = spec["token"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Do not log secret URL tokens.

        def send(self, status, text):
            data = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def safe_origin(self):
            expected = f"127.0.0.1:{self.server.server_address[1]}"
            if self.headers.get("Host") != expected:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin == "http://" + expected

        def do_GET(self):
            parsed = urlparse(self.path)
            supplied = parse_qs(parsed.query).get("t", [""])[0]
            if not self.safe_origin() or not secrets.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
                return self.send(403, "Forbidden")
            if parsed.path != "/":
                return self.send(404, "Not found")
            return self.send(200, page(spec, state["result"]))

        def do_POST(self):
            if not self.safe_origin():
                return self.send(403, "Forbidden origin")
            if self.path != "/decide":
                return self.send(404, "Not found")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 8192:
                    raise ValueError("invalid form length")
                if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                    raise ValueError("invalid form content type")
                form = parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True)
                if any(len(value) != 1 for value in form.values()):
                    raise ValueError("duplicate form fields")
            except (ValueError, UnicodeError):
                return self.send(400, "Invalid form")
            if not secrets.compare_digest(form.get("t", [""])[0].encode("utf-8"), token.encode("utf-8")):
                return self.send(403, "Forbidden")
            try:
                result = decide(cfg, spec, form.get("option", [""])[0], actions=actions, wake=wake)
            except ValueError as error:
                return self.send(400, html.escape(str(error)))
            except Exception as error:
                return self.send(500, "Decision could not complete: " + html.escape(_error(error)))
            state.update(used=True, result=json.dumps(result, ensure_ascii=False, default=str))
            self.send(409 if result["status"] == "used" else 200, page(spec, state["result"]))
            if getattr(self.server, "shutdown_after_decision", False):
                threading.Thread(target=self.server.shutdown, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server, token, state


def main(argv=None):
    parser = argparse.ArgumentParser(description="Request a human decision on a local page")
    parser.add_argument("--config")
    parser.add_argument("kind", choices=["approval", "sql-run", "choice"])
    parser.add_argument("key")
    parser.add_argument("--spec", help="trusted JSON request fields")
    parser.add_argument("--title")
    parser.add_argument("--summary")
    parser.add_argument("--file")
    parser.add_argument("--sql-config")
    parser.add_argument("--expect-host")
    parser.add_argument("--guard-sql")
    parser.add_argument("--guard-expect")
    parser.add_argument("--question")
    parser.add_argument("--options-json")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--url-file")
    parser.add_argument("--timeout", type=float, default=28800)
    parser.add_argument("--retry-wakes", action="store_true")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--request", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except StateDirectoryError as error:
        parser.error(str(error))
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    spec = json.loads(Path(args.spec).read_text()) if args.spec else {}
    spec.update(kind=args.kind, key=args.key)
    for name in ("title", "summary", "file", "expect_host", "guard_sql", "guard_expect", "question"):
        if getattr(args, name) is not None:
            spec[name] = getattr(args, name)
    if args.sql_config:
        spec["config"] = args.sql_config
    if args.options_json:
        spec["options"] = json.loads(args.options_json)
    if args.retry_wakes:
        retry_pending_wakes(cfg)
    if not args.serve:
        spec = create_request(cfg, spec)
        pages = cfg.state_dir / "decision-pages"
        pages.mkdir(parents=True, exist_ok=True)
        pages.chmod(0o700)
        url_file = Path(args.url_file).resolve() if args.url_file else pages / (spec["decision_id"] + ".url")
        url_file.parent.mkdir(parents=True, exist_ok=True)
        # Remove stale readiness before launching this request's fresh server.
        with url_file.open("w") as handle:
            os.chmod(url_file, 0o600)
        log_file = pages / (spec["decision_id"] + ".log")
        command = [sys.executable, "-m", "factory.decision_page", "--config", str(cfg.path),
                   spec["kind"], spec["key"], "--serve", "--request",
                   str(cfg.state_dir / "decisions" / (spec["decision_id"] + ".request.json")),
                   "--url-file", str(url_file), "--timeout", str(args.timeout)]
        if args.open:
            command.append("--open")
        with log_file.open("w") as output:
            os.chmod(log_file, 0o600)
            child = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                     start_new_session=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            text = url_file.read_text().strip()
            if text.startswith("http://127.0.0.1:") and "?t=" in text:
                print(text, flush=True)
                return 0
            if child.poll() is not None:
                raise RuntimeError(f"decision server exited {child.returncode}; see {log_file}")
            time.sleep(0.02)
        child.terminate()
        raise RuntimeError(f"decision server did not start; see {log_file}")
    if args.request:
        spec = json.loads(Path(args.request).read_text())
    server, token, state = make_server(cfg, spec)
    server.shutdown_after_decision = True
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"
    if args.url_file:
        target = Path(args.url_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(url + "\n")
        target.chmod(0o600)
    print(url, flush=True)
    if args.open:
        webbrowser.open(url)
    timer = threading.Timer(args.timeout, server.shutdown)
    timer.daemon = True
    timer.start()
    try:
        server.serve_forever()
    finally:
        timer.cancel()
        server.server_close()
    return 0 if state["used"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
