"""Check committed MySQL migration tables/columns using SELECT queries only.

Supported migrations are CREATE TABLE and ALTER TABLE ADD column, optionally
with USE. Other SQL needs human review. Existence does not verify column types,
constraints, indexes, defaults or data backfills. No migration SQL is executed.
"""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import re

from .check_base import git as git_command
from .config import StateDirectoryError, load_config
from .sql_runner import SQLRefused, _validate, statements


class Unsupported(ValueError):
    """SQL outside the deliberately small migration grammar."""


class ReportableError(ValueError):
    """A fixed message safe to include in a report."""


IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
TOKEN = re.compile(r"\s+|`(?:``|[^`])*`|'(?:''|\\.|[^'\\])*'|"
                   r'"(?:""|\\.|[^"\\])*"|[A-Za-z_][A-Za-z0-9_$]*|'
                   r"[0-9]+(?:\.[0-9]+)?|[(),.=+-]", re.S)
TYPES = set("BIT TINYINT SMALLINT MEDIUMINT INT INTEGER BIGINT DECIMAL DEC NUMERIC FIXED "
            "FLOAT DOUBLE REAL BOOL BOOLEAN DATE DATETIME TIMESTAMP TIME YEAR CHAR VARCHAR "
            "BINARY VARBINARY TINYBLOB BLOB MEDIUMBLOB LONGBLOB TINYTEXT TEXT MEDIUMTEXT "
            "LONGTEXT ENUM SET JSON".split())


class Tokens:
    def __init__(self, sql):
        self.items = []
        offset = 0
        while offset < len(sql):
            match = TOKEN.match(sql, offset)
            if not match:
                raise Unsupported("unsupported SQL token")
            if not match[0].isspace():
                self.items.append(match[0])
            offset = match.end()
        self.pos = 0

    def peek(self):
        return self.items[self.pos].upper() if self.pos < len(self.items) else ""

    def take(self):
        if self.pos == len(self.items):
            raise Unsupported("incomplete migration statement")
        value = self.items[self.pos]
        self.pos += 1
        return value

    def accept(self, value):
        if self.peek() == value:
            self.pos += 1
            return True
        return False

    def expect(self, value):
        if not self.accept(value):
            raise Unsupported("unsupported migration syntax")

    def identifier(self):
        value = self.take()
        if value.startswith("`") and value.endswith("`"):
            value = value[1:-1]
        if not IDENTIFIER.fullmatch(value):
            raise Unsupported("unsupported SQL identifier")
        return value

    def table(self, schema):
        name = self.identifier()
        if self.accept("."):
            schema, name = name, self.identifier()
        if not schema or not isinstance(schema, str) or not IDENTIFIER.fullmatch(schema):
            raise Unsupported("schema is unknown; configure migrations.default_schema or use USE")
        return schema, name

    def done(self):
        if self.peek():
            raise Unsupported("unsupported migration clause")


def _column(tokens, *, alter=False):
    name = tokens.identifier()
    column_type = tokens.take().upper()
    if column_type not in TYPES:
        raise Unsupported("unsupported column type")
    arguments = []
    if tokens.accept("("):
        # Numeric lengths/precision and ENUM/SET literal members only.
        while True:
            value = tokens.take()
            if not (re.fullmatch(r"[0-9]+", value) or value.startswith(("'", '"'))):
                raise Unsupported("unsupported column type arguments")
            arguments.append(value)
            if not tokens.accept(","):
                break
        tokens.expect(")")
    if column_type in ("ENUM", "SET"):
        valid = arguments and all(value.startswith(("'", '"')) for value in arguments)
    else:
        maximum = 2 if column_type in ("DECIMAL", "DEC", "NUMERIC", "FIXED", "FLOAT", "DOUBLE", "REAL") else 1
        valid = len(arguments) <= maximum and all(re.fullmatch(r"[0-9]+", value) for value in arguments)
        if column_type in ("VARCHAR", "VARBINARY"):
            valid = valid and bool(arguments)
    if not valid:
        raise Unsupported("unsupported column type arguments")
    while tokens.peek() and tokens.peek() not in (",", ")"):
        word = tokens.take().upper()
        if word in ("UNSIGNED", "ZEROFILL", "NULL", "AUTO_INCREMENT", "BINARY"):
            continue
        if word == "NOT":
            tokens.expect("NULL")
        elif word in ("PRIMARY", "UNIQUE"):
            if word == "PRIMARY":
                tokens.expect("KEY")
            else:
                tokens.accept("KEY")
        elif word in ("DEFAULT", "COMMENT"):
            if word == "DEFAULT":
                tokens.accept("+") or tokens.accept("-")
            value = tokens.take()
            if word == "COMMENT" and not value.startswith(("'", '"')):
                raise Unsupported("unsupported column comment")
            if not (value.startswith(("'", '"')) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value)
                    or (word == "DEFAULT" and value.upper() in ("NULL", "TRUE", "FALSE", "CURRENT_TIMESTAMP"))):
                raise Unsupported("unsupported column default")
            if value.upper() == "CURRENT_TIMESTAMP" and tokens.accept("("):
                if tokens.peek() != ")" and not re.fullmatch(r"[0-9]+", tokens.take()):
                    raise Unsupported("unsupported timestamp precision")
                tokens.expect(")")
        elif word == "COLLATE":
            tokens.identifier()
        elif word == "CHARACTER":
            tokens.expect("SET")
            tokens.identifier()
        elif alter and word == "AFTER":
            tokens.identifier()
            break
        elif alter and word == "FIRST":
            break
        else:
            raise Unsupported("unsupported column clause")
    return name


def _index_columns(tokens):
    tokens.expect("(")
    while True:
        tokens.identifier()
        if tokens.accept("("):
            if not re.fullmatch(r"[0-9]+", tokens.take()):
                raise Unsupported("unsupported index prefix")
            tokens.expect(")")
        tokens.accept("ASC") or tokens.accept("DESC")
        if not tokens.accept(","):
            break
    tokens.expect(")")


def _table_constraint(tokens):
    if tokens.accept("CONSTRAINT"):
        if tokens.peek() not in ("PRIMARY", "UNIQUE", "FOREIGN"):
            tokens.identifier()
    word = tokens.take().upper()
    if word == "PRIMARY":
        tokens.expect("KEY")
    elif word == "FOREIGN":
        tokens.expect("KEY")
        if tokens.peek() != "(":
            tokens.identifier()
        _index_columns(tokens)
        tokens.expect("REFERENCES")
        # A referenced table can be qualified without changing the current schema.
        tokens.identifier()
        if tokens.accept("."):
            tokens.identifier()
        _index_columns(tokens)
        while tokens.accept("ON"):
            if tokens.take().upper() not in ("DELETE", "UPDATE"):
                raise Unsupported("unsupported foreign key clause")
            action = tokens.take().upper()
            if action == "SET":
                tokens.expect("NULL")
            elif action == "NO":
                tokens.expect("ACTION")
            elif action not in ("CASCADE", "RESTRICT"):
                raise Unsupported("unsupported foreign key action")
        return
    elif word in ("KEY", "INDEX", "UNIQUE"):
        if word == "UNIQUE":
            tokens.accept("KEY") or tokens.accept("INDEX")
        if tokens.peek() != "(":
            tokens.identifier()
    else:
        raise Unsupported("unsupported table constraint")
    _index_columns(tokens)


def parse_migration(sql, default_schema=None):
    """Return existence requirements, refusing the whole file on unknown SQL."""
    requirements = []
    schema = default_schema
    try:
        items = statements(sql)
    except SQLRefused as error:
        raise Unsupported(str(error)) from None
    for item in items:
        tokens = Tokens(item)
        if tokens.accept("USE"):
            schema = tokens.identifier()
            tokens.done()
            continue
        if tokens.accept("CREATE"):
            tokens.expect("TABLE")
            if tokens.accept("IF"):
                tokens.expect("NOT")
                tokens.expect("EXISTS")
            table_schema, table = tokens.table(schema)
            requirements.append({"kind": "table", "schema": table_schema, "table": table})
            tokens.expect("(")
            columns = 0
            seen_columns = set()
            while True:
                if tokens.peek() in ("PRIMARY", "UNIQUE", "KEY", "INDEX", "CONSTRAINT", "FOREIGN", "CHECK"):
                    _table_constraint(tokens)
                else:
                    column = _column(tokens)
                    if column.lower() in seen_columns:
                        raise Unsupported("duplicate CREATE TABLE column")
                    seen_columns.add(column.lower())
                    columns += 1
                    requirements.append({"kind": "column", "schema": table_schema,
                                         "table": table, "column": column})
                if not tokens.accept(","):
                    break
            tokens.expect(")")
            if not columns:
                raise Unsupported("CREATE TABLE has no supported columns")
            while tokens.peek():
                word = tokens.take().upper()
                if word == "DEFAULT":
                    word = tokens.take().upper()
                if word == "CHARACTER":
                    tokens.expect("SET")
                elif word not in ("ENGINE", "CHARSET", "COLLATE", "COMMENT", "AUTO_INCREMENT", "ROW_FORMAT"):
                    raise Unsupported("unsupported CREATE TABLE option")
                tokens.accept("=")
                value = tokens.take()
                if word == "COMMENT":
                    valid = value.startswith(("'", '"'))
                elif word == "AUTO_INCREMENT":
                    valid = re.fullmatch(r"[0-9]+", value)
                else:
                    valid = IDENTIFIER.fullmatch(value)
                if not valid:
                    raise Unsupported("unsupported CREATE TABLE option value")
        elif tokens.accept("ALTER"):
            tokens.expect("TABLE")
            table_schema, table = tokens.table(schema)
            requirements.append({"kind": "table", "schema": table_schema, "table": table})
            while True:
                tokens.expect("ADD")
                tokens.accept("COLUMN")
                if tokens.accept("IF"):
                    tokens.expect("NOT")
                    tokens.expect("EXISTS")
                grouped = tokens.accept("(")
                while True:
                    if tokens.peek() in ("PRIMARY", "UNIQUE", "KEY", "INDEX", "CONSTRAINT", "FOREIGN", "CHECK"):
                        raise Unsupported("ALTER ADD supports columns only")
                    column = _column(tokens, alter=not grouped)
                    requirements.append({"kind": "column", "schema": table_schema,
                                         "table": table, "column": column})
                    if not grouped or not tokens.accept(","):
                        break
                if grouped:
                    tokens.expect(")")
                if not tokens.accept(","):
                    break
        else:
            raise Unsupported("only CREATE TABLE, ALTER TABLE ADD columns and USE are supported")
        tokens.done()
    if items and not requirements:
        raise Unsupported("migration contains no supported schema changes")
    return requirements


def _connect(module, settings):
    return importlib.import_module(module).connect(**settings)


def _settings(cfg):
    migrations = cfg.get("migrations", {})
    settings = cfg.get("decisions", {}).get("sql", {})
    name, expected = migrations.get("sql_config"), migrations.get("expect_host")
    allowed = settings.get("expected_host_prefixes", [])
    forbidden = settings.get("forbidden_host_substrings", [])
    if (not isinstance(name, str) or name not in settings.get("allowed_config_names", [])
            or name not in settings.get("configs", {})):
        raise ReportableError("migration connection config is not allowed")
    if (not isinstance(expected, str) or not expected or not isinstance(allowed, list)
            or not allowed or not all(isinstance(p, str) and p for p in allowed)
            or not any(expected.startswith(p) for p in allowed)
            or not isinstance(forbidden, list) or not all(isinstance(p, str) for p in forbidden)
            or any(p.lower() in expected.lower() for p in forbidden if p)):
        raise ReportableError("expected host is outside the configured host policy")
    module = settings.get("connector_module")
    if not isinstance(module, str) or not module or not isinstance(settings["configs"][name], dict):
        raise ReportableError("invalid migration connector configuration")
    try:
        host_query = _validate(settings.get("host_query", "SELECT @@hostname"), guard=True)[0]
        plain = re.sub(r"'(?:''|\\.|[^'])*'|\"(?:\"\"|\\.|[^\"])*\"|`(?:``|[^`])*`", "''", host_query)
        functions = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", plain)
        if (re.search(r"\b(INTO|FOR|LOCK|PROCEDURE)\b|:=", plain, re.I)
                or re.search(r"[\"`]\s*\(", host_query)
                or any(name.upper() not in ("DATABASE", "VERSION", "CURRENT_DATABASE", "HOST_NAME",
                                            "INET_SERVER_ADDR", "INET_SERVER_PORT", "SERVERPROPERTY")
                       for name in functions)):
            raise SQLRefused("unsupported host query expression")
    except (SQLRefused, TypeError):
        raise ReportableError("host query must be one read-only SELECT") from None
    return module, settings["configs"][name], expected, allowed, forbidden, host_query


def _probe(cfg, requirements, connect, report):
    module, options, expected, allowed, forbidden, host_query = _settings(cfg)
    connection = cursor = None
    try:
        connection = connect(module, options)
        cursor = connection.cursor()
        cursor.execute(host_query)
        row = cursor.fetchone()
        if not row or row[0] is None:
            raise ReportableError("host query returned no host")
        host = str(row[0])
        report["host"] = host
        if (not host.startswith(expected) or not any(host.startswith(p) for p in allowed)
                or any(p.lower() in host.lower() for p in forbidden if p)):
            raise ReportableError("connected host is outside the approved host policy")
        for requirement in requirements:
            # All object names have passed IDENTIFIER. Literal quoting avoids a
            # dependency on connector-specific DB-API parameter marker styles.
            schema, table = requirement["schema"], requirement["table"]
            if requirement["kind"] == "table":
                query = ("SELECT 1 FROM information_schema.tables WHERE table_schema = '" + schema +
                         "' AND table_name = '" + table + "' AND table_type = 'BASE TABLE'")
            else:
                query = ("SELECT 1 FROM information_schema.columns WHERE table_schema = '" + schema +
                         "' AND table_name = '" + table + "' AND column_name = '" + requirement["column"] + "'")
            cursor.execute(query)
            if not cursor.fetchone():
                report["missing"].append(requirement)
    finally:
        # Close even after a rejected host. Never commit or execute migration SQL.
        try:
            if cursor is not None:
                cursor.close()
        finally:
            if connection is not None:
                connection.close()


def check(cfg, *, repo=None, base=None, head=None, git=None, connect=None):
    """Return (JSON-safe report, exit code); Git and DB are injectable offline.

    git(repo, stage, *argv) returns stdout or raises. connect(module, options)
    returns a DB-API connection. Neither dependency is called for unknown SQL.
    """
    report = {"check": "migrations", "status": "ERROR", "migrations": [],
              "requirements": [], "missing": [], "host": None}
    git, connect = git or git_command, connect or _connect
    try:
        settings = cfg.get("migrations", {})
        repo = repo if repo is not None else settings.get("repo", ".")
        path = Path(repo).expanduser()
        path = path if path.is_absolute() else cfg.project_root / path
        base = base or settings.get("base", cfg.get("base_branch", "origin/main"))
        head = head or settings.get("head", "HEAD")
        pattern = settings.get("glob", "**/*.sql")
        if not all(isinstance(value, str) and value and "\0" not in value for value in (base, head, pattern)):
            raise ReportableError("invalid migration refs or glob")
        report.update(repo=str(path), base=base, head=head)
        shas = []
        for label, ref in (("base", base), ("head", head)):
            sha = git(path, "resolve-" + label, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").strip()
            if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
                raise ReportableError("invalid migration commit SHA")
            shas.append(sha)
        report.update(base_head=shas[0], branch_head=shas[1])
        paths = git(path, "migration-files", "diff", "--name-only", "--diff-filter=AM",
                    "--no-renames", "-z", shas[0], shas[1], "--", ":(glob)" + pattern)
        if paths and not paths.endswith("\0"):
            raise ReportableError("invalid migration file list")
        report["migrations"] = sorted(set(paths.rstrip("\0").split("\0"))) if paths else []
        if not report["migrations"]:
            report.update(status="PASS", reason="no-relevant-migrations")
            return report, 0
        default_schema = settings.get("default_schema")
        if "default_schema" not in settings:
            sql = cfg.get("decisions", {}).get("sql", {})
            options = sql.get("configs", {}).get(settings.get("sql_config"), {})
            default_schema = options.get("database", options.get("dbname"))
        for filename in report["migrations"]:
            content = git(path, "migration-content", "show", shas[1] + ":" + filename)
            try:
                requirements = parse_migration(content, default_schema)
            except Unsupported:
                report["unsupported_file"] = filename
                raise
            for requirement in requirements:
                if requirement not in report["requirements"]:
                    report["requirements"].append(requirement)
        if report["requirements"]:
            _probe(cfg, report["requirements"], connect, report)
        code = 1 if report["missing"] else 0
        report.update(status="FAIL" if code else "PASS", reason="missing-schema-objects" if code else "applied")
        return report, code
    except Unsupported as error:
        report.update(status="HUMAN", reason=str(error))
        return report, 3
    except Exception as error:
        report.update(status="ERROR", reason=str(error) if isinstance(error, ReportableError)
                      else type(error).__name__ + ": migration environment operation failed")
        return report, 10


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ReportableError("invalid-arguments; see --help")


def main(argv=None):
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config")
    parser.add_argument("--repo")
    parser.add_argument("--base")
    parser.add_argument("--head")
    try:
        args = parser.parse_args(argv)
        report, code = check(load_config(args.config), repo=args.repo, base=args.base, head=args.head)
    except Exception as error:
        report = {"check": "migrations", "status": "ERROR", "reason": str(error)
                  if isinstance(error, (ReportableError, StateDirectoryError)) else "invalid-config"}
        code = 10
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
