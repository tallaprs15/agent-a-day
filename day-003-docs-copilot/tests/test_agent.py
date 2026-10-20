import asyncio
import os
import shutil
import subprocess
import sys

import pytest
from google.adk.agents import SequentialAgent

from docs_copilot.agent import (
    ASK_FOR_QUESTION,
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
from docs_copilot.ledger import RunLedger, unverified_sources


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    """Stand-in for ADK's callback context: session state plus the user's message."""

    def __init__(self, state=None, text=None):
        from google.genai import types

        self.state = dict(state or {})
        self.user_content = types.Content(role="user", parts=[types.Part(text=text)]) if text else None


def _ledger(**kw):
    return RunLedger(model="gemini-2.5-flash", **kw)


def test_before_tool_enforces_budget():
    before_tool, _, _ = _make_callbacks(_ledger(max_tool_calls=1))
    assert before_tool(_Tool("query-docs"), {"libraryId": "/a/b", "query": "q"}, None) is None
    assert "budget" in before_tool(_Tool("query-docs"), {"libraryId": "/a/b", "query": "q"}, None)["error"]


def test_before_tool_caps_query_length():
    before_tool, _, _ = _make_callbacks(_ledger())
    args = {"query": "x" * 5000, "libraryName": "y" * 5000}
    assert before_tool(_Tool("resolve-library-id"), args, None) is None
    assert len(args["query"]) == QUERY_MAX_CHARS and len(args["libraryName"]) == QUERY_MAX_CHARS


def test_query_docs_rejects_a_non_context7_library_id_without_spending_budget():
    ledger = _ledger()
    before_tool, _, _ = _make_callbacks(ledger)
    result = before_tool(_Tool("query-docs"), {"libraryId": "fastapi", "query": "q"}, None)
    assert "resolve-library-id" in result["error"]
    assert ledger.tool_calls == 0


def test_after_tool_truncates_and_records_only_what_the_model_sees():
    ledger = _ledger()
    _, after_tool, _ = _make_callbacks(ledger)
    big = {"content": [{"type": "text", "text": "A" * (TOOL_RESULT_MAX_CHARS + 500) + "SECRET_TAIL"}]}
    replaced = after_tool(_Tool("query-docs"), {}, None, big)
    assert replaced["content"][0]["text"].endswith("[truncated]")
    assert "SECRET_TAIL" not in ledger.evidence[0]  # the model never saw it, so it can't count as evidence
    small = {"content": [{"type": "text", "text": "ok"}]}
    assert after_tool(_Tool("query-docs"), {}, None, small) is None
    assert _truncate_result("not a dict") == ("not a dict", False)


def test_after_tool_evidence_keeps_urls_followed_by_newlines_matchable():
    """Context7 puts a newline right after each source URL; JSON-escaping it once broke source checks."""
    ledger = _ledger()
    _, after_tool, _ = _make_callbacks(ledger)
    after_tool(_Tool("query-docs"), {}, None, {"content": [{"type": "text", "text": "Source: https://fastapi.tiangolo.com/tutorial/dependencies/\n\nDeclare"}]})
    answer = "## Answer\nUse Depends.\n\n## Sources\n- https://fastapi.tiangolo.com/tutorial/dependencies/"
    assert unverified_sources(answer, ledger) == []


def test_question_comes_from_the_message_not_import_time():
    """Two questions in one session: each message rebinds the pipeline."""
    ledger = _ledger()
    resolve = _make_pipeline_callbacks(ledger)
    ctx = _Ctx(text="How do I declare a dependency in FastAPI?")
    assert resolve(ctx) is None
    assert ctx.state["question"].startswith("How do I declare") and ledger.question == ctx.state["question"]
    ctx2 = _Ctx(state=ctx.state, text="How do I upload a blob with the Azure Storage SDK?")
    assert resolve(ctx2) is None and "Azure" in ctx2.state["question"] and "Azure" in ledger.question


def test_each_question_gets_a_fresh_budget_and_evidence():
    """`adk web` shares one ledger across messages; a spent budget or old evidence must not carry over."""
    ledger = _ledger()
    resolve = _make_pipeline_callbacks(ledger)
    resolve(_Ctx(text="How do I declare a dependency in FastAPI?"))
    for _ in range(ledger.max_tool_calls):
        ledger.record_tool_call("query-docs")
    ledger.record_evidence("https://fastapi.tiangolo.com/tutorial/dependencies/")
    resolve(_Ctx(text="How do I read a CSV file with pandas?"))
    assert ledger.tool_calls == 0 and ledger.calls_by_tool == {} and ledger.evidence == []
    assert ledger.record_tool_call("query-docs")


def test_asks_for_a_question_when_none_given():
    reply = _make_pipeline_callbacks(_ledger())(_Ctx(text="hello"))
    assert reply.parts[0].text == ASK_FOR_QUESTION


def test_pipeline_is_sequential_with_state_handoff():
    agent = build_agent(_ledger())
    assert isinstance(agent, SequentialAgent)
    assert [a.name for a in agent.sub_agents] == ["researcher", "answerer"]
    assert [a.output_key for a in agent.sub_agents] == ["snippets", "answer"]
    assert agent.sub_agents[1].tools == []  # the answerer has no tools: it can only use the snippets
    assert "{question}" in agent.sub_agents[0].instruction
    assert "{snippets}" in agent.sub_agents[1].instruction


def _tool_names(toolset):
    async def names():
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    return asyncio.run(names())


@pytest.mark.skipif(shutil.which("npx") is None, reason="npx not installed")
def test_context7_exposes_exactly_the_filtered_tools():
    """Starts the real Context7 server (no API key needed)."""
    assert _tool_names(_context7_toolset()) == sorted(CONTEXT7_TOOLS)


def test_microsoft_learn_exposes_exactly_the_filtered_tools():
    """Calls the real remote Microsoft Learn server (no API key needed)."""
    assert _tool_names(_learn_toolset()) == sorted(LEARN_TOOLS)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_answer_has_verified_sources_and_grounded_code(tmp_path):
    out = tmp_path / "answer.md"
    result = subprocess.run(
        [sys.executable, "-m", "docs_copilot", "How do I upload a local file to Azure Blob Storage using the Python SDK?", "-o", str(out)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    answer = out.read_text()
    assert "## Answer" in answer and "## Sources" in answer
