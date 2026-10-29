import asyncio
import json
import os
import subprocess
import sys

import pytest
from google.adk.agents import SequentialAgent
from google.genai import types

from model_picker.agent import (
    ASK_FOR_REQUEST,
    HF_MCP_URL,
    HF_TOOLS,
    Judgements,
    _hf_toolset,
    _make_render,
    _make_resolve_request,
    _make_tool_callbacks,
    build_agent,
)
from model_picker.ledger import RunLedger


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    """Stand-in for ADK's callback context: session state and the user message."""

    def __init__(self, text=None, state=None):
        self.state = state if state is not None else {}
        self.user_content = types.Content(role="user", parts=[types.Part(text=text)]) if text else None


def _ledger(**kw):
    return RunLedger(model="gemini-2.5-flash", **kw)


def test_before_tool_enforces_budget():
    ledger = _ledger(max_tool_calls=1)
    before_tool, _, _ = _make_tool_callbacks(ledger)
    assert before_tool(_Tool("hub_repo_search"), {"query": "a"}, _Ctx()) is None
    assert "budget" in before_tool(_Tool("hub_repo_search"), {"query": "b"}, _Ctx())["error"]


def test_before_tool_caps_and_pins_arguments():
    before_tool, _, _ = _make_tool_callbacks(_ledger())
    search = {"query": "x" * 1000, "limit": 100, "repo_types": ["dataset", "space"]}
    assert before_tool(_Tool("hub_repo_search"), search, _Ctx()) is None
    assert len(search["query"]) == 200 and search["limit"] == 8 and search["repo_types"] == ["model"]
    details = {"repo_ids": [f"a/m{i}" for i in range(9)], "operations": ["dataset_preview"]}
    assert before_tool(_Tool("hub_repo_details"), details, _Ctx()) is None
    assert len(details["repo_ids"]) == 5 and details["operations"] == ["overview"] and details["repo_type"] == "model"


def test_after_tool_records_facts_from_results():
    ledger = _ledger()
    _, after_tool, _ = _make_tool_callbacks(ledger)
    text = "### acme/m\n\n**Task:** x | **Downloads:** 5K | **Likes:** 2\n\n**Tags:** license:mit\n"
    after_tool(_Tool("hub_repo_search"), {}, _Ctx(), {"content": [{"text": text}]})
    assert ledger.facts["acme/m"].downloads == 5000 and ledger.facts["acme/m"].licence == "mit"


def test_request_comes_from_each_message_not_import_time():
    """Two different requests in two turns of one session each bind to state."""
    resolve = _make_resolve_request()
    ctx = _Ctx(text="Small English sentiment classifier, Apache-2.0")
    assert resolve(ctx) is None and "sentiment" in ctx.state["request"]
    ctx2 = _Ctx(text="Speech recognition for Spanish on a phone", state=ctx.state)
    assert resolve(ctx2) is None and "Spanish" in ctx2.state["request"] and "sentiment" not in ctx2.state["request"]


def test_missing_request_triggers_a_clarifying_question():
    resolve = _make_resolve_request()
    assert resolve(_Ctx(text="hi")).parts[0].text == ASK_FOR_REQUEST
    assert resolve(_Ctx(text=None)).parts[0].text == ASK_FOR_REQUEST


def test_render_ranks_with_code_and_replaces_the_judges_json():
    ledger = _ledger()
    ledger.record_evidence("### acme/m\n\n**Downloads:** 2M | **Likes:** 2\n\n**Tags:** license:mit\n\n**Created:** 1 Jan, 2026\n")
    judged = Judgements(candidates=[
        {"repo_id": "acme/m", "task_fit": 5, "constraint_fit": 4, "rationale": "Fits.", "caveat": ""},
        {"repo_id": "ghost/model", "task_fit": 5, "constraint_fit": 5, "rationale": "Invented.", "caveat": ""},
    ])
    ctx = _Ctx(state={"request": "need a thing", "judgement": judged.model_dump()})
    reply = _make_render(ledger)(ctx)
    assert "acme/m" in reply.parts[0].text and "ghost/model" not in reply.parts[0].text
    assert [s.repo_id for s in ledger.ranked] == ["acme/m"] and any("ghost/model" in p for p in ledger.problems)


def test_pipeline_shape_is_scout_then_judge():
    root = build_agent(_ledger())
    assert isinstance(root, SequentialAgent)
    scout, judge = root.sub_agents
    assert [scout.name, judge.name] == ["scout", "judge"]
    assert len(scout.tools) == 1 and not judge.tools  # only the scout touches the outside world
    assert judge.output_schema is Judgements


def test_mcp_server_exposes_exactly_the_filtered_tools():
    """Connects to the real Hugging Face MCP server (no account needed) and checks least-privilege filtering."""

    async def names():
        toolset = _hf_toolset()
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    assert HF_MCP_URL.startswith("https://huggingface.co/")
    assert asyncio.run(names()) == sorted(HF_TOOLS) == ["hub_repo_details", "hub_repo_search"]


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_shortlist_is_ranked_from_tool_results(tmp_path):
    out, metrics = tmp_path / "shortlist.md", tmp_path / "m.json"
    result = subprocess.run(
        [
            sys.executable, "-m", "model_picker",
            "Multilingual sentence embeddings for semantic search, permissive licence, small enough for a laptop CPU",
            "-o", str(out), "--metrics", str(metrics),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    m = json.loads(metrics.read_text())
    assert m["passed"] and m["checks"]["all_ranked_repos_from_tools"] and m["checks"]["ranked"] >= 2
    assert "## Ranking" in out.read_text()
