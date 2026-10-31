import asyncio
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from paper_debate import agent as A
from paper_debate.ledger import RunLedger


class _Tool:
    def __init__(self, name):
        self.name = name


def _ctx(text=""):
    content = SimpleNamespace(parts=[SimpleNamespace(text=text)])
    return SimpleNamespace(user_content=content, state={})


def test_before_tool_enforces_budget_and_caps_search():
    ledger = RunLedger(model="m", max_tool_calls=1)
    before_tool, _, _ = A._make_tool_callbacks(ledger)
    args = {"query": "x" * 1000, "max_results": 50}
    assert before_tool(_Tool("search_papers"), args, None) is None
    assert len(args["query"]) == A.QUERY_MAX and args["max_results"] == A.SEARCH_MAX_RESULTS
    assert "budget" in before_tool(_Tool("get_abstract"), {"paper_id": "1"}, None)["error"]


def test_resolve_paper_asks_when_no_id_and_rebinds_per_message():
    resolve = A._make_resolve_paper()
    ctx = _ctx("what is a transformer?")
    assert "arXiv ID" in resolve(ctx).parts[0].text and ctx.state == {}
    # Two different inputs in one session: the second message re-binds everything.
    ctx = _ctx("Paper: https://arxiv.org/abs/1706.03762v7")
    assert resolve(ctx) is None and ctx.state["paper_id"] == "1706.03762"
    ctx.state["transcript"] = "old debate"
    ctx.user_content = SimpleNamespace(parts=[SimpleNamespace(text="Paper: 2005.14165")])
    assert resolve(ctx) is None
    assert ctx.state["paper_id"] == "2005.14165" and ctx.state["transcript"] == A.NO_ARGUMENTS


def test_transcript_accumulates_turns_in_order():
    ledger = RunLedger(model="m")
    start, adv_end, skp_end = A._make_debate_callbacks(ledger)
    ctx = SimpleNamespace(state={"transcript": A.NO_ARGUMENTS, "advocate_turn": "A1", "skeptic_turn": "S1"})
    start(ctx), adv_end(ctx), skp_end(ctx)
    t = ctx.state["transcript"]
    assert t.index("Round 1 · Advocate") < t.index("A1") < t.index("Round 1 · Skeptic") < t.index("S1")
    assert A.NO_ARGUMENTS not in t


def test_agent_graph_and_instructions_are_not_hard_coded():
    root = A.build_agent(RunLedger(model="m"), rounds=3)
    assert [a.name for a in root.sub_agents] == ["reader", "debate", "judge", "reporter"]
    debate = root.sub_agents[1]
    assert debate.max_iterations == 3 and [a.name for a in debate.sub_agents] == ["advocate", "skeptic"]
    assert root.sub_agents[2].output_schema is A.Ruling
    for text in (A.READER_INSTRUCTION, A.ADVOCATE_INSTRUCTION, A.SKEPTIC_INSTRUCTION, A.JUDGE_INSTRUCTION):
        assert "1706" not in text  # the paper always comes from state


def test_render_report_from_state():
    state = {
        "paper_id": "1706.03762",
        "brief": "C1: a",
        "transcript": "### Round 1 · Advocate\nx",
        "ruling": {"claims": [{"claim_id": "C1", "verdict": "holds", "rationale": "a|b"}], "summary": "S", "confidence": "low"},
    }
    md = A.render_report(state)
    assert "arXiv:1706.03762" in md and "| C1 | ✅ holds | a/b |" in md and "confidence: low" in md


needs_uvx = pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")


@needs_uvx
@pytest.mark.parametrize("tools", [A.READER_TOOLS, A.SKEPTIC_TOOLS])
def test_mcp_server_exposes_exactly_the_filtered_tools(tools):
    """Starts the real arXiv MCP server (no API key needed) and checks least-privilege filtering."""

    async def names():
        toolset = A._arxiv_toolset(tools)
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    assert asyncio.run(names()) == sorted(tools)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_debate_passes_all_checks(tmp_path):
    out, metrics = tmp_path / "report.md", tmp_path / "m.json"
    result = subprocess.run(
        [sys.executable, "-m", "paper_debate", "1706.03762", "-o", str(out), "--metrics", str(metrics)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    report = out.read_text()
    assert "## Ruling" in report and "Round 2 · Skeptic" in report
