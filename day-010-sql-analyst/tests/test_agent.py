import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

from sql_analyst.agent import ASK_FOR_QUESTION, DEFAULT_DB, SQLITE_TOOLS, _make_callbacks, _sqlite_toolset
from sql_analyst.ledger import RunLedger


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    def __init__(self, text):
        from google.genai import types

        self.user_content = types.Content(role="user", parts=[types.Part(text=text)])
        self.state = {}


def test_before_tool_rejects_writes_without_spending_budget():
    ledger = RunLedger(model="m", max_tool_calls=3)
    before_tool = _make_callbacks(ledger)[1]
    blocked = before_tool(_Tool("read_query"), {"query": "DELETE FROM Track"}, None)
    assert "guardrail" in blocked["error"]
    assert ledger.tool_calls == 0 and ledger.rejected_sql == ["DELETE FROM Track"] and ledger.executed_sql == []


def test_before_tool_caps_rows_records_sql_and_enforces_budget():
    ledger = RunLedger(model="m", max_tool_calls=2)
    before_tool = _make_callbacks(ledger)[1]
    args = {"query": "SELECT * FROM Track;"}
    assert before_tool(_Tool("read_query"), args, None) is None
    assert args["query"].endswith("LIMIT 100") and ledger.executed_sql == ["SELECT * FROM Track"]
    assert before_tool(_Tool("list_tables"), {}, None) is None
    assert "budget" in before_tool(_Tool("read_query"), {"query": "SELECT 1"}, None)["error"]


def test_question_comes_from_the_message_not_the_code():
    """Two different questions in one session bind to two different states; a greeting asks instead."""
    ledger = RunLedger(model="m")
    resolve_question = _make_callbacks(ledger)[0]
    first, second = _Ctx("Top 5 genres by track count"), _Ctx("Which country spends the most?")
    assert resolve_question(first) is None and resolve_question(second) is None
    assert first.state["question"] == "Top 5 genres by track count"
    assert second.state["question"] == "Which country spends the most?" and ledger.question == second.state["question"]
    assert resolve_question(_Ctx("hi")).parts[0].text == ASK_FOR_QUESTION


needs_uvx = pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")


@needs_uvx
def test_sqlite_server_exposes_exactly_the_filtered_tools():
    """Starts the real MCP server (no API key needed) and checks least-privilege filtering."""

    async def names():
        toolset = _sqlite_toolset()
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    assert asyncio.run(names()) == sorted(SQLITE_TOOLS)


@needs_uvx
def test_guarded_query_runs_through_the_real_server_and_is_row_capped():
    """The wrapped SELECT is accepted by the real server and returns at most 100 rows."""
    from sql_analyst.guard import cap_rows, check_read_only

    async def rows():
        toolset = _sqlite_toolset()
        try:
            tool = next(t for t in await toolset.get_tools() if t.name == "read_query")
            sql = cap_rows(check_read_only("SELECT TrackId, Name FROM Track"))
            return await tool.run_async(args={"query": sql}, tool_context=None)
        finally:
            await toolset.close()

    result = asyncio.run(rows())
    text = json.dumps(result, default=str)
    assert text.count("TrackId") == 100


def test_bundled_database_is_unchanged_by_the_suite():
    conn = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
    assert conn.execute("SELECT COUNT(*) FROM Track").fetchone() == (3503,)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_answer_is_grounded_and_database_untouched(tmp_path):
    out, metrics = tmp_path / "answer.md", tmp_path / "m.json"
    result = subprocess.run(
        [sys.executable, "-m", "sql_analyst", "Which 5 genres have the most tracks, and how many tracks does each have?",
         "-o", str(out), "--metrics", str(metrics)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    answer = out.read_text()
    assert "Rock" in answer and "1297" in answer.replace(",", "")  # Chinook ground truth
    checks = json.loads(metrics.read_text())["checks"]
    assert checks["shown_sql_was_executed"] is True
    conn = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
    assert conn.execute("SELECT COUNT(*) FROM Track").fetchone() == (3503,)
