"""Argument guardrail for SQL: only a single, read-only SELECT may reach the database.

The MCP server's `read_query` tool is *supposed* to take SELECTs only, but the check lives in the server's
prompt, not in code that we control. This module is the code we control: it runs in `before_tool_callback`,
rejects anything that is not one SELECT, and wraps the statement so the result can never exceed a row cap.
"""

from __future__ import annotations

import re

MAX_ROWS = 100
MAX_SQL_CHARS = 2000

# Whole words that must never appear in an analyst query, even inside a SELECT (SQLite allows
# `WITH ... DELETE`, `SELECT load_extension(...)`, `PRAGMA`, `ATTACH`, ...).
FORBIDDEN = (
    "insert", "update", "delete", "drop", "alter", "create", "replace", "truncate", "attach", "detach",
    "pragma", "vacuum", "reindex", "analyze", "begin", "commit", "rollback", "savepoint", "release",
    "load_extension", "writefile", "readfile", "fts3_tokenizer",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN) + r")\b", re.IGNORECASE)
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


class SqlRejected(ValueError):
    """The query is not allowed; the message is shown to the model so it can fix the query."""


def check_read_only(sql: str) -> str:
    """Return the normalised SELECT (no trailing semicolon) or raise SqlRejected."""
    text = (sql or "").strip()
    if not text:
        raise SqlRejected("Empty query.")
    if len(text) > MAX_SQL_CHARS:
        raise SqlRejected(f"Query is longer than {MAX_SQL_CHARS} characters; simplify it.")
    # Literals are data ('Delete me' is a fine search term), so strip them before looking at keywords.
    code = _STRING_LITERAL.sub("''", text)
    if "--" in code or "/*" in code:
        raise SqlRejected("SQL comments are not allowed.")
    code = code.rstrip().rstrip(";").rstrip()
    if ";" in code:
        raise SqlRejected("Only one statement per call.")
    if not re.match(r"(?is)^\s*(select|with)\b", code):
        raise SqlRejected("Only SELECT queries are allowed (read-only analyst).")
    hit = _FORBIDDEN_RE.search(code)
    if hit:
        raise SqlRejected(f"'{hit.group(1).upper()}' is not allowed; this agent can only read data.")
    return text.rstrip().rstrip(";").rstrip()


def cap_rows(sql: str, limit: int = MAX_ROWS) -> str:
    """Wrap the query so it returns at most `limit` rows, whatever the model wrote."""
    return f"SELECT * FROM (\n{sql}\n) LIMIT {limit}"
