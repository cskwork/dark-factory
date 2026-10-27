"""Planner-readable ticket comment gate: lint before posting, verify after read-back.

Ticket comments are read by planners and QA. Developer detail (queries, flow
charts, commits, builds, file and code names) belongs in the developer note
adapter, not the ticket. Input is an Atlassian Document Format (ADF) document,
markdown/plain text, or a tracker view JSON with `fields.comment.comments`.

  python3 -m factory.comment_check [--config CONFIG] lint comment.adf.json
  ADAPTER_VIEW | python3 -m factory.comment_check readback --comment ID --min-media 1 --min-mention 1

Prints one JSON object; exit 0 passed, 1 failed, 2 invalid input.
"""
import argparse
import json
import os
import re
import sys

from .config import load_config

MAX_CHARS = 800  # Comment body characters without whitespace, mentions and media. Descriptions use 0 (off).

TERMS = ["API", "SQL", "JSON", "HTTP", "JWT", "curl", "endpoint", "payload", "null", "true", "false",
         "SELECT", "UPDATE", "INSERT", "DELETE", "JOIN", "commit", "merge", "rebase", "build",
         "branch", "query", "schema", "column", "index", "migration", "pod", "token", "stack trace"]

RULES = [
    ("commit hash", r"(?<![\w-])(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![\w-])"),
    ("build number", r"(?<!\w)#\d{2,6}\b"),
    ("branch name", r"\b(?:fix|feat|feature|hotfix|merge|release)/[\w.-]+"),
    ("code name", r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b|\b[a-z][a-z0-9]*_[a-z0-9_]+\b"
                  r"|\b[a-z]+(?:[A-Z][a-z0-9]+)+\b|\b[A-Z][a-z]+(?:[A-Z][a-z0-9]+){2,}\b"
                  r"|\b[\w-]+\.(?:java|kt|xml|vue|tsx?|jsx?|py|go|rb|sql|ya?ml|toml)\b"),
    ("comment id reference", r"(?i)\bcomment\s*#?\d{4,}"),
]

# Raw markdown or JSON inside a posted document means conversion failed.
LEAKS = ("**", "|---", '{"type"', "](http")


class InputError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError("invalid-arguments; see --help")


def settings(cfg):
    comments = cfg.get("comments", {}) if cfg else {}
    if not isinstance(comments, dict):
        raise InputError("invalid comments config")
    max_chars = comments.get("max_chars", MAX_CHARS)
    terms, allow = comments.get("terms", []), comments.get("allow", [])
    patterns = comments.get("patterns", {})
    if type(max_chars) is not int or max_chars < 0:
        raise InputError("invalid comments.max_chars")
    if any(not isinstance(v, list) or not all(isinstance(s, str) and s for s in v) for v in (terms, allow)):
        raise InputError("invalid comments.terms or comments.allow")
    if not isinstance(patterns, dict) or not all(isinstance(v, str) for v in patterns.values()):
        raise InputError("invalid comments.patterns")
    for pattern in patterns.values():
        re.compile(pattern)
    return {"max_chars": max_chars, "terms": TERMS + terms, "allow": allow, "patterns": patterns}


def collect(node, found):
    if isinstance(node, dict):
        kind = node.get("type")
        found["nodes"][kind] = found["nodes"].get(kind, 0) + 1
        if kind == "text":
            found["texts"].append(node.get("text", ""))
            found["marks"].update(m.get("type") for m in node.get("marks", []) if isinstance(m, dict))
        elif kind == "media":
            attrs = node.get("attrs", {})
            found["media"].append(attrs.get("alt") or attrs.get("id") or "media")
        for child in node.get("content", []) or []:
            collect(child, found)
    return found


def parse(document):
    """Return texts, node counts, marks and media for ADF or markdown/plain text."""
    found = {"texts": [], "nodes": {}, "marks": set(), "media": []}
    if isinstance(document, dict):
        return collect(document, found), True
    text = str(document)
    if re.search(r"^\s*(```|~~~)", text, re.M):
        found["nodes"]["codeBlock"] = 1
    if re.search(r"(?<!`)`[^`\n]+`(?!`)", text):
        found["marks"].add("code")
    found["media"] = re.findall(r"!\[([^\]]*)\]\([^)]+\)", text)
    found["nodes"]["mention"] = len(re.findall(r"@\[[^\]]+\]\([^)]+\)", text))
    # Link and image targets are not prose; keep their labels only.
    found["texts"].append(re.sub(r"!?\[([^\]]*)\]\([^)]+\)", r"\1", text))
    return found, False


def lint(document, options, max_chars=None):
    """Return failures [{rule, examples}]; empty means planner-readable."""
    found, adf = parse(document)
    body = "\n".join(found["texts"])
    allow = set(options["allow"])
    terms = sorted(set(options["terms"]) - allow, key=len, reverse=True)
    rules = RULES + sorted(options["patterns"].items())
    if terms:
        # ASCII boundaries only: particles attach to non-Latin terms (e.g. Korean).
        rules.append(("developer term", r"(?i)(?<![A-Za-z0-9_])(?:" + "|".join(map(re.escape, terms)) + r")(?![A-Za-z0-9_])"))
    failures = []
    for name, pattern in rules:
        hits = sorted({m.group(0) for m in re.finditer(pattern, body)} - allow)
        if hits:
            failures.append({"rule": name, "examples": hits[:5]})
    if found["nodes"].get("codeBlock"):
        failures.append({"rule": "code block", "examples": ["move queries and commands to the developer note"]})
    if "code" in found["marks"]:
        failures.append({"rule": "code format", "examples": ["name the screen or menu path instead"]})
    if adf:
        leaks = [s for s in LEAKS if any(s in t for t in found["texts"])]
        if leaks:
            failures.append({"rule": "unconverted markup", "examples": leaks})
    limit = options["max_chars"] if max_chars is None else max_chars
    chars = len(re.sub(r"\s+", "", body))
    if limit and chars > limit:
        failures.append({"rule": "length", "examples": [f"{chars} > {limit} characters"]})
    return failures, found


def load_document(path):
    with open(path, encoding="utf-8") as stream:
        text = stream.read()
    if path.endswith(".json"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise InputError("invalid JSON document") from None
    return text


def pick(view, comment_id, description):
    """Select one body from a tracker view, or accept a bare ADF document."""
    if not isinstance(view, dict):
        raise InputError("invalid read-back JSON")
    if "fields" not in view:
        if view.get("type") != "doc":
            raise InputError("read-back is neither a tracker view nor an ADF document")
        return view, {}
    fields = view["fields"]
    if description:
        return fields.get("description") or {}, {}
    comments = (fields.get("comment") or {}).get("comments") or []
    match = next((c for c in comments if str(c.get("id")) == comment_id), None)
    if match is None:
        raise InputError("comment not found: " + comment_id)
    author = match.get("author") or {}
    return match.get("body") or {}, {"author": author.get("displayName"), "updated": match.get("updated")}


def main(argv=None):
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config")
    parser.add_argument("--allow", action="append", default=[], help="term confirmed as planner vocabulary")
    parser.add_argument("--max-chars", type=int, help=f"body character cap (default {MAX_CHARS}; 0 disables)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("lint").add_argument("document")
    readback = commands.add_parser("readback")
    target = readback.add_mutually_exclusive_group(required=True)
    target.add_argument("--comment")
    target.add_argument("--description", action="store_true")
    readback.add_argument("--min-media", type=int, default=0)
    readback.add_argument("--min-mention", type=int, default=0)
    try:
        args = parser.parse_args(argv)
        path = args.config or os.environ.get("FACTORY_CONFIG")
        options = settings(load_config(path) if path else None)
        options["allow"] += args.allow
        meta, max_chars = {}, args.max_chars
        if args.command == "lint":
            document = load_document(args.document)
        else:
            try:
                view = json.load(sys.stdin)
            except json.JSONDecodeError:
                raise InputError("invalid read-back JSON") from None
            document, meta = pick(view, args.comment, args.description)
            if args.description and max_chars is None:
                max_chars = 0
        failures, found = lint(document, options, max_chars)
        if args.command == "readback":
            mentions = found["nodes"].get("mention", 0)
            if len(found["media"]) < args.min_media:
                failures.append({"rule": "media", "examples": [f"{len(found['media'])} < {args.min_media}"]})
            if mentions < args.min_mention:
                failures.append({"rule": "mention", "examples": [f"{mentions} < {args.min_mention}"]})
    except (InputError, OSError, re.error, ValueError) as error:
        print(json.dumps({"check": "comment", "status": "FAIL", "error": str(error)}, ensure_ascii=False))
        return 2
    report = {"check": "comment", "status": "FAIL" if failures else "PASS", **meta,
              "media": len(found["media"]), "mentions": found["nodes"].get("mention", 0), "failures": failures}
    print(json.dumps(report, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
