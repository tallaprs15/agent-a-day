import asyncio
import os
import subprocess
import sys

import pytest
from google.adk.agents import Agent
from google.genai import types

from cf_docs_helper.agent import (
    ASK_FOR_QUESTION,
    DOCS_TOOLS,
    MAX_QUERY_CHARS,
    _docs_toolset,
    _make_callbacks,
    build_agent,
    root_agent,
)
from cf_docs_helper.ledger import RunLedger


class _Tool:
    def __init__(self, name="search_cloudflare_documentation"):
        self.name = name


class _Ctx:
    def __init__(self, text=None):
        self.user_content = types.Content(role="user", parts=[types.Part(text=text)]) if text else None


def _ledger(**kw):
    return RunLedger(model="gemini-2.5-flash", **kw)


def test_before_tool_enforces_budget():
    _, before_tool, _, _ = _make_callbacks(_ledger(max_tool_calls=1))
    assert before_tool(_Tool(), {"query": "kv limits"}, None) is None
    assert "budget" in before_tool(_Tool(), {"query": "kv limits"}, None)["error"]


def test_before_tool_caps_query_length_and_rejects_empty():
    ledger = _ledger()
    _, before_tool, _, _ = _make_callbacks(ledger)
    args = {"query": "x" * 5000}
    assert before_tool(_Tool(), args, None) is None
    assert len(args["query"]) == MAX_QUERY_CHARS
    assert "required" in before_tool(_Tool(), {"query": "  "}, None)["error"]
    assert ledger.tool_calls == 1  # the rejected call was free


def test_after_tool_records_returned_doc_paths():
    ledger = _ledger()
    _, _, after_tool, _ = _make_callbacks(ledger)
    after_tool(_Tool(), {}, None, {"content": [{"text": "<url>https://developers.cloudflare.com/kv/api/</url>"}]})
    assert "/kv/api" in ledger.seen_paths


def test_after_model_meters_tokens():
    ledger = _ledger()
    *_, after_model = _make_callbacks(ledger)

    class _Resp:
        usage_metadata = types.GenerateContentResponseUsageMetadata(prompt_token_count=10, candidates_token_count=4)

    after_model(None, _Resp())
    assert (ledger.model_calls, ledger.prompt_tokens, ledger.output_tokens) == (1, 10, 4)


def test_empty_message_asks_for_a_question_without_a_model_call():
    check, *_ = _make_callbacks(_ledger())
    assert check(_Ctx()).parts[0].text == ASK_FOR_QUESTION
    assert check(_Ctx("How do I bind a KV namespace?")) is None


def test_agent_has_one_filtered_remote_tool_and_no_hardcoded_subject():
    agent = build_agent(_ledger())
    assert isinstance(agent, Agent) and agent is not root_agent
    assert len(agent.tools) == 1 and agent.tools[0].tool_filter == DOCS_TOOLS
    # The question is the user's message; nothing about a particular product is templated in.
    assert "{" not in agent.instruction


def _tool_names(toolset):
    async def names():
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    return asyncio.run(names())


def test_cloudflare_docs_server_exposes_exactly_the_filtered_tools():
    """Calls the real remote Cloudflare Docs server (no API key needed)."""
    assert _tool_names(_docs_toolset()) == sorted(DOCS_TOOLS)


def test_search_returns_doc_urls_the_ledger_can_verify():
    """Real search call: result pages must be recognised by the evidence ledger."""

    async def search():
        toolset = _docs_toolset()
        try:
            (tool,) = await toolset.get_tools()
            return await tool.run_async(args={"query": "Workers KV limits"}, tool_context=None)
        finally:
            await toolset.close()

    result = asyncio.run(search())
    ledger = _ledger()
    import json

    ledger.record_evidence(json.dumps(result, default=str))
    assert any(p.startswith("/kv") for p in ledger.seen_paths), ledger.seen_paths


QUESTIONS = ["What are the Workers KV limits?", "How do I create a Queue consumer in Workers?"]


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_answer_has_sections_and_verified_links(tmp_path):
    out = tmp_path / "answer.md"
    result = subprocess.run(
        [sys.executable, "-m", "cf_docs_helper", QUESTIONS[0], "-o", str(out)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    answer = out.read_text()
    assert "## Answer" in answer and "developers.cloudflare.com" in answer
