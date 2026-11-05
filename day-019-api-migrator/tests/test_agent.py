import asyncio
import os
import shutil
import subprocess
import sys

import pytest
from google.adk.agents import ParallelAgent, SequentialAgent

from api_migrator.agent import (
    ASK_FOR_REQUEST,
    CONTEXT7_TOOLS,
    LEARN_TOOLS,
    QUERY_MAX_CHARS,
    TOOL_RESULT_MAX_CHARS,
    _context7_toolset,
    _learn_toolset,
    _make_callbacks,
    _make_pipeline_callbacks,
    _truncate_result,
    build_agent,
)
from api_migrator.ledger import RunLedger


class _Tool:
    def __init__(self, name):
        self.name = name


class _ToolCtx:
    def __init__(self, agent_name):
        self.agent_name = agent_name


class _Ctx:
    """Stand-in for ADK's callback context: session state plus the user's message."""

    def __init__(self, state=None, text=None):
        from google.genai import types

        self.state = dict(state or {})
        self.user_content = types.Content(role="user", parts=[types.Part(text=text)]) if text else None


def _ledger(**kw):
    return RunLedger(model="gemini-2.5-flash", **kw)


def test_before_tool_enforces_budget_across_both_researchers():
    before_tool, _, _ = _make_callbacks(_ledger(max_tool_calls=1))
    args = {"libraryId": "/a/b", "query": "q"}
    assert before_tool(_Tool("query-docs"), args, _ToolCtx("old_researcher")) is None
    assert "budget" in before_tool(_Tool("query-docs"), args, _ToolCtx("new_researcher"))["error"]


def test_before_tool_caps_query_length():
    before_tool, _, _ = _make_callbacks(_ledger())
    args = {"query": "x" * 5000, "libraryName": "y" * 5000}
    assert before_tool(_Tool("resolve-library-id"), args, None) is None
    assert len(args["query"]) == QUERY_MAX_CHARS and len(args["libraryName"]) == QUERY_MAX_CHARS


def test_query_docs_rejects_a_non_context7_library_id_without_spending_budget():
    ledger = _ledger()
    before_tool, _, _ = _make_callbacks(ledger)
    assert "resolve-library-id" in before_tool(_Tool("query-docs"), {"libraryId": "pydantic"}, None)["error"]
    assert ledger.tool_calls == 0


def test_after_tool_files_evidence_under_the_calling_side():
    ledger = _ledger()
    _, after_tool, _ = _make_callbacks(ledger)
    after_tool(_Tool("query-docs"), {}, _ToolCtx("old_researcher"), {"content": [{"type": "text", "text": "OLD_API"}]})
    after_tool(_Tool("query-docs"), {}, _ToolCtx("new_researcher"), {"content": [{"type": "text", "text": "NEW_API"}]})
    assert "OLD_API" in ledger.seen("old") and "OLD_API" not in ledger.seen("new")
    assert "NEW_API" in ledger.seen("new") and "NEW_API" not in ledger.seen("old")


def test_after_tool_truncates_and_records_only_what_the_model_sees():
    ledger = _ledger()
    _, after_tool, _ = _make_callbacks(ledger)
    big = {"content": [{"type": "text", "text": "A" * (TOOL_RESULT_MAX_CHARS + 500) + "SECRET_TAIL"}]}
    replaced = after_tool(_Tool("query-docs"), {}, _ToolCtx("new_researcher"), big)
    assert replaced["content"][0]["text"].endswith("[truncated]")
    assert "SECRET_TAIL" not in ledger.seen("new")
    assert _truncate_result("not a dict") == ("not a dict", False)


def test_request_comes_from_the_message_not_import_time():
    """Two migrations in one session: each message rebinds the pipeline."""
    ledger = _ledger()
    resolve = _make_pipeline_callbacks(ledger)
    ctx = _Ctx(text="Migrate Pydantic from v1 to v2")
    assert resolve(ctx) is None and "Pydantic" in ctx.state["request"] and "Pydantic" in ledger.request
    ctx2 = _Ctx(state=ctx.state, text="Upgrade the Azure Storage Blob SDK from 2.1 to 12")
    assert resolve(ctx2) is None and "Azure" in ctx2.state["request"] and "Azure" in ledger.request


def test_each_request_gets_a_fresh_budget_and_evidence():
    ledger = _ledger()
    resolve = _make_pipeline_callbacks(ledger)
    resolve(_Ctx(text="Migrate Pydantic from v1 to v2"))
    for _ in range(ledger.max_tool_calls):
        ledger.record_tool_call("query-docs")
    ledger.record_evidence("old", "stale")
    resolve(_Ctx(text="Move requests from 2.0 to 3.0"))
    assert ledger.tool_calls == 0 and ledger.seen() == ""


def test_asks_for_library_and_versions_when_missing():
    resolve = _make_pipeline_callbacks(_ledger())
    assert resolve(_Ctx(text="hello")).parts[0].text == ASK_FOR_REQUEST
    assert resolve(_Ctx(text="Migrate Pydantic please")).parts[0].text == ASK_FOR_REQUEST


def test_pipeline_is_parallel_gather_then_toolless_differ():
    agent = build_agent(_ledger())
    assert isinstance(agent, SequentialAgent)
    gather, differ = agent.sub_agents
    assert isinstance(gather, ParallelAgent)
    assert [a.name for a in gather.sub_agents] == ["old_researcher", "new_researcher"]
    assert [a.output_key for a in gather.sub_agents] == ["old_docs", "new_docs"]
    assert differ.tools == [] and differ.output_key == "plan"  # the differ can only use the two digests
    for needle in ("{request}", "{old_docs}", "{new_docs}"):
        assert needle in differ.instruction
    assert all("{request}" in a.instruction for a in gather.sub_agents)


def _tool_names(toolset):
    async def names():
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    return asyncio.run(names())


@pytest.mark.skipif(shutil.which("npx") is None, reason="npx not installed")
def test_context7_exposes_exactly_the_filtered_tools():
    """Starts the real Context7 server (no API key needed to list tools)."""
    assert _tool_names(_context7_toolset()) == sorted(CONTEXT7_TOOLS)


def test_microsoft_learn_exposes_exactly_the_filtered_tools():
    """Calls the real remote Microsoft Learn server (no API key needed)."""
    assert _tool_names(_learn_toolset()) == sorted(LEARN_TOOLS)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_plan_is_grounded_in_both_doc_sets(tmp_path):
    out = tmp_path / "plan.md"
    result = subprocess.run(
        [sys.executable, "-m", "api_migrator",
         "Migrate the Azure Storage Blob Python SDK from the legacy 2.1 BlockBlobService to the current v12 BlobServiceClient",
         "-o", str(out)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    plan = out.read_text()
    assert "## Breaking changes" in plan and "## Sources" in plan
