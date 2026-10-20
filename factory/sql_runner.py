"""Execute a reviewed SQL snapshot through a configured DB-API connector.

Only transactional data statements are supported. DDL and transaction commands
are refused because some databases implicitly commit them before rollback.
"""
from __future__ import annotations

import importlib
import re
from typing import Any


class SQLRefused(ValueError):
    """A safe explanation produced by this module, never by the driver."""


def statements(text: str) -> list[str]:
    """Split ordinary SQL, preserving quoted semicolons and dropping comments.

    Dollar quoting, executable comments and nonstandard delimiters are refused.
    Both backslash escapes and doubled quote escapes are recognized.
    """
    output, buf = [], []
    quote = None
    i = 0
    while i < len(text):
        char = text[i]
        following = text[i:i + 2]
        if quote:
            buf.append(char)
            if char == "\\":
                i += 1
                if i >= len(text):
                    raise SQLRefused("unterminated SQL escape")
                buf.append(text[i])
            elif char == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    i += 1
                    buf.append(text[i])
                else:
                    quote = None
        elif char in "'\"`":
            quote = char
            buf.append(char)
        elif following == "--" or char == "#":
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            buf.append(" ")
            continue
        elif following == "/*":
            if text[i:i + 3] in ("/*!", "/*+"):
                raise SQLRefused("executable comments and hints are not supported")
            end = text.find("*/", i + 2)
            if end < 0:
                raise SQLRefused("unterminated SQL comment")
            i = end + 2
            buf.append(" ")
            continue
        elif char == "$":
            raise SQLRefused("dollar quoting and placeholders are not supported")
        elif char == ";":
            if "".join(buf).strip():
                output.append("".join(buf).strip())
            buf = []
        else:
            buf.append(char)
        i += 1
    if quote:
        raise SQLRefused("unterminated SQL quote")
    if "".join(buf).strip():
        output.append("".join(buf).strip())
    return output


def _validate(text: str, *, guard: bool = False) -> list[str]:
    items = statements(text)
    if not items or (guard and len(items) != 1):
        raise SQLRefused("SQL must contain statements; a guard must be one SELECT")
    for item in items:
        keyword = re.match(r"[A-Za-z]+", item)
        allowed = {"SELECT"} if guard else {"SELECT", "INSERT", "UPDATE", "DELETE"}
        if not keyword or keyword[0].upper() not in allowed:
            raise SQLRefused("only transactional SELECT/INSERT/UPDATE/DELETE are supported")
        # SELECT INTO can write external files; locks and session changes belong
        # in a separately reviewed database-specific executor.
        plain = re.sub(r"'(?:''|\\.|[^'])*'|\"(?:\"\"|\\.|[^\"])*\"|`(?:``|[^`])*`", "''", item)
        if re.search(r"\b(INTO\s+(OUTFILE|DUMPFILE)|FOR\s+UPDATE|LOCK\s+IN\s+SHARE)\b|:=", plain, re.I):
            raise SQLRefused("SQL file writes, session assignments and explicit locks are unsupported")
    return items


def run_connection(conn: Any, *, expect_host: str, sql_text: str,
                   allowed_host_prefixes: list[str], forbidden_host_substrings: list[str],
                   host_query: str, guard_sql: str, guard_expect: Any) -> dict:
    """Verify identity and guard on the very connection that runs the SQL.

    A shared endpoint can route connections to different servers; checking a
    separate connection would not establish where these statements will run.
    """
    result = {"status": "refused", "host": None, "guard": None, "steps": []}
    cursor = None
    try:
        sql = _validate(sql_text)
        guard = _validate(guard_sql, guard=True)[0]
        host_stmt = _validate(host_query, guard=True)[0]
        if not expect_host or not allowed_host_prefixes or not any(
                expect_host.startswith(prefix) for prefix in allowed_host_prefixes if prefix):
            raise SQLRefused("expected host prefix is outside configured allowlist")
        autocommit = getattr(conn, "autocommit", None)
        if callable(autocommit):
            autocommit(False)
        elif autocommit is True:
            conn.autocommit = False
        cursor = conn.cursor()
        if not getattr(conn, "in_transaction", False):
            cursor.execute("BEGIN")
        cursor.execute(host_stmt)
        row = cursor.fetchone()
        if not row:
            raise SQLRefused("host query returned no row")
        host = str(row[0])
        result["host"] = host
        if (not host.startswith(expect_host) or
                not any(host.startswith(p) for p in allowed_host_prefixes if p) or
                any(word.lower() in host.lower() for word in forbidden_host_substrings if word)):
            raise SQLRefused("connected host is outside the approved host policy")
        cursor.execute(guard)
        row = cursor.fetchone()
        if not row:
            raise SQLRefused("guard query returned no row")
        result["guard"] = row[0]
        if str(row[0]) != str(guard_expect):
            raise SQLRefused(f"guard returned {row[0]!r}, expected {guard_expect!r}")
        for item in sql:
            step = {"sql": item}
            result["steps"].append(step)
            cursor.execute(item)
            step["rowcount"] = cursor.rowcount
        conn.commit()
        result["status"] = "done"
    except Exception as error:
        result["reason"] = (str(error) if isinstance(error, SQLRefused) else
                            type(error).__name__ + ": database operation failed")
        if result["steps"] or not isinstance(error, SQLRefused):
            result["status"] = "failed"
        try:
            conn.rollback()
        except Exception as rollback_error:
            result["rollback_error"] = type(rollback_error).__name__ + ": rollback failed"
            result["status"] = "failed"
    finally:
        if cursor is not None and hasattr(cursor, "close"):
            try:
                cursor.close()
            except Exception as cleanup_error:
                result["cleanup_error"] = type(cleanup_error).__name__ + ": cursor close failed"
    return result


def run(cfg, *, config_name: str, sql_text: str, expect_host: str,
        guard_sql: str, guard_expect: Any) -> dict:
    settings = cfg.get("decisions", {}).get("sql", {})
    if config_name not in settings.get("allowed_config_names", []):
        return {"status": "refused", "reason": "connection config is not allowed", "steps": []}
    conn, result = None, None
    try:
        connector = importlib.import_module(settings["connector_module"])
        conn = connector.connect(**settings["configs"][config_name])
        result = run_connection(conn, expect_host=expect_host, sql_text=sql_text,
                                allowed_host_prefixes=settings.get("expected_host_prefixes", []),
                                forbidden_host_substrings=settings.get("forbidden_host_substrings", []),
                                host_query=settings.get("host_query", "SELECT @@hostname"),
                                guard_sql=guard_sql, guard_expect=guard_expect)
    except Exception as error:
        result = {"status": "failed", "reason": type(error).__name__ + ": connector operation failed", "steps": []}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as cleanup_error:
                result["connection_cleanup_error"] = type(cleanup_error).__name__ + ": connection close failed"
    return result
