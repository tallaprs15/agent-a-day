import asyncio
import os
import shutil

import pytest
from google.adk.agents import SequentialAgent
from google.adk.runners import InMemoryRunner
from google.genai import types

from model_router.agent import (
    ASK_FOR_REQUEST,
    FETCH_TOOLS,
    Assessment,
    RouteDispatcher,
    _fetch_toolset,
    _make_resolve_request,
    _make_tool_callbacks,
    assessment_dict,
    build_agent,
)
from model_router.ledger import RunLedger


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    def __init__(self, text=None, state=None):
        self.state = state if state is not None else {}
        self.user_content = types.Content(role="user", parts=[types.Part(text=text)]) if text else None


def _ledger(**kw):
    return RunLedger(lite_model="gemini-3.5-flash-lite", strong_model="gemini-3.8-flash", **kw)


def test_before_tool_caps_fetch_and_enforces_budget():
    before_tool, _ = _make_tool_callbacks(_ledger(max_tool_calls=1))
    args = {"url": "https://example.com", "max_length": 50_000}
    assert before_tool(_Tool("fetch"), args, None) is None
    assert args["max_length"] == 8000
    assert "budget" in before_tool(_Tool("fetch"), {"url": "https://example.com"}, None)["error"]


def test_before_tool_refuses_private_urls_without_spending_budget():
    ledger = _ledger(max_tool_calls=3)
    before_tool, _ = _make_tool_callbacks(ledger)
    assert "refused" in before_tool(_Tool("fetch"), {"url": "http://169.254.169.254/"}, None)["error"]
    assert ledger.tool_calls == 0


def test_after_tool_counts_failed_tool_calls():
    ledger = _ledger()
    _, after_tool = _make_tool_callbacks(ledger)
    after_tool(_Tool("fetch"), {}, None, {"content": [{"text": "ok"}]})
    after_tool(_Tool("fetch"), {}, None, "Error executing tool fetch: Failed to fetch robots.txt")
    assert ledger.tool_errors == 1


def test_server_env_forwards_only_proxy_and_ca_settings(monkeypatch):
    from model_router.agent import _server_env

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    monkeypatch.setenv("GOOGLE_API_KEY", "placeholder")
    env = _server_env()
    assert env["HTTPS_PROXY"] == "http://proxy:3128" and "GOOGLE_API_KEY" not in env


def test_resolve_request_stores_request_and_clears_stale_assessment():
    ledger, state = _ledger(), {"assessment": {"reasoning_depth": 3}}
    assert _make_resolve_request(ledger)(_Ctx("Explain how TCP slow start works", state)) is None
    assert state["request"] == "Explain how TCP slow start works" and state["assessment"] == ""
    assert ledger.request == state["request"]


def test_resolve_request_asks_when_too_short():
    reply = _make_resolve_request(_ledger())(_Ctx("hi"))
    assert reply.parts[0].text == ASK_FOR_REQUEST


def test_assessment_dict_accepts_dict_or_json_string():
    assert assessment_dict('{"multi_step": 2}') == {"multi_step": 2}
    assert assessment_dict({"multi_step": 2}) == {"multi_step": 2}
    assert assessment_dict("not json") == {} and assessment_dict(None) == {}


def test_pipeline_shape_router_is_lite_and_never_has_tools():
    root = build_agent(_ledger())
    assert isinstance(root, SequentialAgent)
    router, dispatcher = root.sub_agents
    assert router.model == "gemini-3.5-flash-lite" and router.output_schema is Assessment and not router.tools
    assert isinstance(dispatcher, RouteDispatcher)
    lite, strong = dispatcher.sub_agents
    assert (lite.name, lite.model) == ("lite_worker", "gemini-3.5-flash-lite")
    assert (strong.name, strong.model) == ("strong_worker", "gemini-3.8-flash")


def test_root_agent_never_hardcodes_a_task():
    from model_router.agent import ROUTER_INSTRUCTION, WORKER_INSTRUCTION, root_agent

    assert root_agent.name == "model_router"
    for text in (ROUTER_INSTRUCTION, WORKER_INSTRUCTION):
        assert "{request}" in text and "http" not in text.replace("`fetch`", "")


needs_uvx = pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")


@needs_uvx
def test_mcp_server_exposes_exactly_the_filtered_tools():
    """Starts the real MCP server (no API key needed) and checks least-privilege filtering."""

    async def names():
        toolset = _fetch_toolset()
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    assert asyncio.run(names()) == sorted(FETCH_TOOLS)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_two_requests_in_one_session_route_independently():
    """Same session, two requests: an easy lookup goes to lite, a hard one to strong, and the state doesn't leak."""
    hard = (
        "Design a lock-free multi-producer single-consumer queue in Rust and prove it is linearizable. "
        "Explain every memory-ordering choice and the ABA hazards, step by step."
    )
    easy = "What is the capital of Australia?"

    async def go():
        ledger = _ledger(max_tool_calls=4)
        runner = InMemoryRunner(agent=build_agent(ledger), app_name="t")
        session = await runner.session_service.create_session(app_name="t", user_id="u")
        routes = []
        try:
            for text in (easy, hard):
                async for _ in runner.run_async(
                    user_id="u", session_id=session.id, new_message=types.Content(role="user", parts=[types.Part(text=text)])
                ):
                    pass
                routes.append((ledger.request, ledger.route.tier, ledger.route.total))
        finally:
            await runner.close()
        return routes

    (q1, tier1, _), (q2, tier2, _) = asyncio.run(go())
    assert q1 == easy and q2 == hard
    assert tier1 == "lite" and tier2 == "strong"

