import asyncio
import os
import shutil
import subprocess
import sys

import pytest
from google.adk.runners import InMemoryRunner
from google.genai import types

from competitor_scan.agent import (
    DUCKDUCKGO_TOOLS,
    FETCH_MAX_LENGTH,
    FETCH_TOOLS,
    SEARCH_MAX_RESULTS,
    Renderer,
    _make_resolve_market,
    _make_tool_callbacks,
    _stdio,
    build_agent,
    DUCKDUCKGO_SERVER,
    FETCH_SERVER,
)
from competitor_scan.ledger import Landscape, RunLedger


class _Tool:
    def __init__(self, name):
        self.name = name


def test_search_args_are_capped():
    before_tool, _, _ = _make_tool_callbacks(RunLedger(model="m"))
    args = {"query": "x" * 500, "max_results": 50}
    assert before_tool(_Tool("search"), args, None) is None
    assert len(args["query"]) == 200 and args["max_results"] == SEARCH_MAX_RESULTS


def test_fetch_is_limited_to_searched_sites_and_capped():
    led = RunLedger(model="m")
    before_tool, after_tool, _ = _make_tool_callbacks(led)
    refused = before_tool(_Tool("fetch"), {"url": "https://evil.example/"}, None)
    assert "limited" in refused["error"] and led.tool_calls == 0  # refusal is free
    assert after_tool(_Tool("search"), {}, None, {"result": "URL: https://acme.example/"}) is None
    args = {"url": "https://acme.example/pricing", "max_length": 99999, "raw": True}
    assert before_tool(_Tool("fetch"), args, None) is None
    assert args["max_length"] == FETCH_MAX_LENGTH and args["raw"] is False
    for article in ("https://acme.example/blog/best-flags", "https://github.com/acme/acme", "https://acme.example/compare/x"):
        assert "vendor pages only" in before_tool(_Tool("fetch"), {"url": article}, None)["error"]
    assert led.tool_calls == 1  # only the pricing fetch above was counted
    paging = {"url": "https://acme.example/pricing", "start_index": 6000}
    assert "already requested" in before_tool(_Tool("fetch"), paging, None)["error"]
    other = {"url": "https://acme.example/", "start_index": 6000}
    assert before_tool(_Tool("fetch"), other, None) is None and other["start_index"] == 0


def test_fetch_result_is_rewritten_as_numbered_passages():
    led = RunLedger(model="m")
    _, after_tool, _ = _make_tool_callbacks(led)
    page = {"content": [{"type": "text", "text": "Acme is an open-source feature flag platform for teams. Plans start at $20 per seat."}]}
    out = after_tool(_Tool("fetch"), {"url": "https://acme.example/pricing"}, None, page)
    assert out["url"] == "https://acme.example/pricing"
    assert out["passages"][0].startswith("[P1] Acme is an open-source") and out["passages"][1].startswith("[P2] Plans start")
    assert led.has_url("https://acme.example/pricing")


def test_failed_fetch_leaves_no_passages():
    led = RunLedger(model="m")
    _, after_tool, _ = _make_tool_callbacks(led)
    assert after_tool(_Tool("fetch"), {"url": "https://acme.example/"}, None, {"error": "robots.txt forbids it"}) is None
    assert not led.passages


def test_tool_budget_blocks_calls():
    led = RunLedger(model="m", max_tool_calls=1)
    before_tool, _, _ = _make_tool_callbacks(led)
    assert before_tool(_Tool("search"), {"query": "q"}, None) is None
    assert "budget" in before_tool(_Tool("search"), {"query": "q"}, None)["error"]


def test_after_model_meters_tokens():
    led = RunLedger(model="m")
    _, _, after_model = _make_tool_callbacks(led)

    class R:
        usage_metadata = types.GenerateContentResponseUsageMetadata(prompt_token_count=10, candidates_token_count=4)

    after_model(None, R())
    assert (led.model_calls, led.prompt_tokens, led.output_tokens) == (1, 10, 4)


def test_pipeline_shape_and_tool_isolation():
    agent = build_agent(RunLedger(model="m"))
    assert [a.name for a in agent.sub_agents] == ["discover", "extract", "normalise", "render"]
    discover, extract, normalise, render = agent.sub_agents
    assert len(discover.tools) == 1 and len(extract.tools) == 1 and not normalise.tools
    assert normalise.output_schema is Landscape and isinstance(render, Renderer)


def test_missing_market_asks_a_question_and_spends_nothing():
    class Ctx:
        user_content = types.Content(role="user", parts=[types.Part(text="hi")])
        state = {}

    reply = _make_resolve_market(RunLedger(model="m"))(Ctx())
    assert "Which market" in reply.parts[0].text and Ctx.state == {}


def test_market_is_read_from_each_message_and_resets_the_ledger():
    led = RunLedger(model="m")
    led.record_tool_call("search")
    resolve = _make_resolve_market(led)

    class Ctx:
        def __init__(self, text):
            self.user_content = types.Content(role="user", parts=[types.Part(text=text)])
            self.state = {}

    first, second = Ctx("open-source feature flag platforms"), Ctx("  password   managers for families ")
    assert resolve(first) is None and first.state["market"] == "open-source feature flag platforms"
    assert led.tool_calls == 0
    assert resolve(second) is None and second.state["market"] == "password managers for families"


def test_renderer_drops_unverifiable_rows_without_a_model():
    led = RunLedger(model="m")
    led.record_search("URL: https://acme.example/")
    led.record_page("https://acme.example/pricing", "Acme is an open-source feature flag platform for teams.")
    good = dict(
        name="Acme", url="https://acme.example/pricing", what_it_is="Flags.", pricing_model="Unknown", starting_price="Unknown",
        open_source="Yes", differentiator="Unknown", what_it_is_passage="P1", pricing_passage="",
    )
    bad = {**good, "name": "Ghost", "url": "https://ghost.example/", "what_it_is_passage": "P99"}
    landscape = Landscape.model_validate({"market": "flags", "products": [good, bad]})
    agent = build_agent(led)

    async def run():
        runner = InMemoryRunner(agent=agent.sub_agents[-1], app_name="t")
        session = await runner.session_service.create_session(app_name="t", user_id="u", state={"landscape": landscape.model_dump()})
        out = ""
        async for ev in runner.run_async(user_id="u", session_id=session.id, new_message=types.Content(role="user", parts=[types.Part(text="go")])):
            if ev.content and ev.content.parts:
                out += ev.content.parts[0].text or ""
        return out

    out = asyncio.run(run())
    assert "[Acme]" in out and "[Ghost]" not in out and "Not verified" in out and "Ghost" in out
    assert '"Acme is an open-source feature flag platform for teams."' in out


needs_uvx = pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")


@needs_uvx
@pytest.mark.parametrize(
    "package,expected", [(DUCKDUCKGO_SERVER, DUCKDUCKGO_TOOLS), (FETCH_SERVER, FETCH_TOOLS)]
)
def test_mcp_servers_expose_exactly_the_filtered_tools(package, expected):
    """Starts the real MCP servers (no API key needed) and checks least-privilege filtering."""

    async def names():
        toolset = _stdio(package, expected)
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    assert asyncio.run(names()) == sorted(expected)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_scan_produces_a_verified_table(tmp_path):
    out, metrics = tmp_path / "table.md", tmp_path / "m.json"
    result = subprocess.run(
        [sys.executable, "-m", "competitor_scan", "open-source feature flag platforms for small teams",
         "-o", str(out), "--metrics", str(metrics)],
        capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stderr
    text = out.read_text()
    assert "| Product |" in text and "## Evidence" in text
