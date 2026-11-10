import asyncio
import json
import os
import subprocess
import sys

import pytest
from google.adk.agents import Agent
from google.genai import types

from dataset_scout.__main__ import run_checks
from dataset_scout.agent import (
    ASK_FOR_REQUEST,
    HF_MCP_URL,
    HF_TOOLS,
    INSTRUCTION,
    _hf_toolset,
    _make_callbacks,
    _make_resolve_request,
    build_agent,
)
from dataset_scout.ledger import RunLedger


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
    before_tool, _, _ = _make_callbacks(ledger)
    assert before_tool(_Tool("hub_repo_search"), {"query": "a"}, _Ctx()) is None
    assert "budget" in before_tool(_Tool("hub_repo_search"), {"query": "b"}, _Ctx())["error"]


def test_before_tool_pins_datasets_and_caps_arguments():
    before_tool, _, _ = _make_callbacks(_ledger())
    search = {"query": "x" * 1000, "limit": 100, "repo_types": ["model", "space"]}
    assert before_tool(_Tool("hub_repo_search"), search, _Ctx()) is None
    assert len(search["query"]) == 200 and search["limit"] == 8 and search["repo_types"] == ["dataset"]
    details = {"repo_ids": [f"a/d{i}" for i in range(9)], "operations": ["dataset_preview", "hf_fs"], "limit": 99}
    assert before_tool(_Tool("hub_repo_details"), details, _Ctx()) is None
    assert len(details["repo_ids"]) == 3 and details["repo_type"] == "dataset"
    assert details["operations"] == ["dataset_preview"] and details["limit"] == 3
    empty = {"repo_ids": ["a/d"], "operations": ["nonsense"]}
    before_tool(_Tool("hub_repo_details"), empty, _Ctx())
    assert empty["operations"] == ["overview"]


def test_after_tool_records_evidence_and_truncates_big_results():
    ledger = _ledger()
    _, after_tool, _ = _make_callbacks(ledger)
    small = {"content": [{"type": "text", "text": "### acme/d\n\n**Downloads:** 5\n"}]}
    assert after_tool(_Tool("hub_repo_search"), {}, _Ctx(), small) is None  # unchanged
    assert "acme/d" in ledger.seen_datasets
    big = {"content": [{"type": "text", "text": "# acme/e\n" + "row " * 10_000}]}
    capped = after_tool(_Tool("hub_repo_details"), {}, _Ctx(), big)
    assert len(capped["content"][0]["text"]) < 9100 and "acme/e" in ledger.seen_datasets


def test_request_comes_from_each_message_not_import_time():
    """Two different requests in two turns of one session each bind to state."""
    resolve = _make_resolve_request()
    ctx = _Ctx(text="English sentiment data for a small classifier, Apache-2.0")
    assert resolve(ctx) is None and "sentiment" in ctx.state["request"]
    ctx2 = _Ctx(text="Spanish speech recordings for ASR fine-tuning", state=ctx.state)
    assert resolve(ctx2) is None and "Spanish" in ctx2.state["request"] and "sentiment" not in ctx2.state["request"]


def test_missing_request_triggers_a_clarifying_question():
    resolve = _make_resolve_request()
    assert resolve(_Ctx(text="hi")).parts[0].text == ASK_FOR_REQUEST
    assert resolve(_Ctx(text=None)).parts[0].text == ASK_FOR_REQUEST


def test_agent_shape_and_instruction_is_templated_not_hardcoded():
    agent = build_agent(_ledger())
    assert isinstance(agent, Agent) and len(agent.tools) == 1
    assert "{request}" in INSTRUCTION
    assert "huggingface.co/datasets/author/name" in INSTRUCTION  # only a format example, no real dataset


def test_checks_fail_on_invented_dataset_and_missing_structure():
    ledger = _ledger()
    ledger.record_evidence("### acme/one\n### acme/two\n")
    ledger.record_tool_call("hub_repo_details", {"operations": ["overview", "dataset_structure"]})
    ledger.guide = (
        "[a](https://huggingface.co/datasets/acme/one) [b](https://huggingface.co/datasets/acme/two)\n"
        'load_dataset("acme/one")'
    )
    checks, bad = run_checks(ledger)
    assert all(checks.values()) and bad == []
    ledger.guide += " [c](https://huggingface.co/datasets/ghost/x)"
    checks, bad = run_checks(ledger)
    assert not checks["citations_verified"] and bad == ["ghost/x"]
    ledger.structure_calls = 0
    assert not run_checks(ledger)[0]["structure_inspected"]


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
def test_live_guide_cites_only_datasets_from_tool_results(tmp_path):
    out, metrics = tmp_path / "guide.md", tmp_path / "m.json"
    result = subprocess.run(
        [
            sys.executable, "-m", "dataset_scout",
            "Fine-tune a small model to classify customer support tickets by intent, English, permissive licence",
            "-o", str(out), "--metrics", str(metrics),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    m = json.loads(metrics.read_text())
    assert m["passed"] and m["checks"]["citations_verified"] and m["checks"]["structure_inspected"]
    assert "## Shortlist" in out.read_text()
