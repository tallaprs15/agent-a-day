import asyncio
import os
import shutil
import subprocess
import sys

import pytest
from google.adk.agents import Agent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from a2a_duo.agent import ARXIV_TOOLS, _arxiv_toolset, _librarian_callbacks, build_librarian, build_writer, card_url
from a2a_duo.ledger import RunLedger
from a2a_duo.server import BackgroundServer


class _Tool:
    def __init__(self, name):
        self.name = name


def test_librarian_before_tool_enforces_budget_and_caps_search():
    ledger = RunLedger(model="m", max_tool_calls=1)
    before_tool, _ = _librarian_callbacks(ledger)
    args = {"query": "x", "max_results": 50}
    assert before_tool(_Tool("search_papers"), args, None) is None
    assert args["max_results"] == 8
    assert "budget" in before_tool(_Tool("get_abstract"), {}, None)["error"]


def test_librarian_agent_has_only_the_filtered_toolset():
    agent = build_librarian(RunLedger(model="gemini-3.5-flash-lite"))
    assert [t.tool_filter for t in agent.tools] == [ARXIV_TOOLS]


class ScriptedLlm(BaseLlm):
    """Deterministic stand-in for Gemini: replays a fixed script of responses."""

    model: str = "scripted"
    script: list = []
    calls: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        content = step(llm_request) if callable(step) else step
        yield LlmResponse(content=content)


def _text(t):
    return types.Content(role="model", parts=[types.Part(text=t)])


def _call(name, **args):
    return types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))])


def _last_function_response_text(req):
    for content in reversed(req.contents):
        for part in content.parts or []:
            if part.function_response:
                return str(part.function_response.response)
    return ""


def test_writer_reaches_librarian_over_a2a_and_ledger_traces_ids():
    """Real A2A over localhost (agent card, JSON-RPC, RemoteA2aAgent); only the LLMs are scripted."""
    ledger = RunLedger(model="scripted")
    librarian = Agent(
        name="librarian",
        model=ScriptedLlm(script=[_text("- arXiv:2401.00001 | Paper A | shows A")]),
        description="stub librarian",
        instruction="reply",
    )

    async def go():
        async with BackgroundServer(ledger, agent=librarian) as server:
            writer = build_writer(ledger, card_url(server.port))
            writer.model = ScriptedLlm(
                script=[
                    _call("librarian", request="topic X"),
                    lambda req: _text(f"Brief citing {_last_function_response_text(req)[:200]}"),
                ]
            )
            runner = InMemoryRunner(agent=writer, app_name="t")
            session = await runner.session_service.create_session(app_name="t", user_id="u")
            out = ""
            async for ev in runner.run_async(
                user_id="u", session_id=session.id, new_message=types.Content(role="user", parts=[types.Part(text="Topic: X")])
            ):
                if ev.is_final_response() and ev.content:
                    out = "".join(p.text or "" for p in ev.content.parts)
            await runner.close()
            return out

    out = asyncio.run(go())
    assert "2401.00001" in out
    assert ledger.delegations == 1
    assert ledger.librarian_ids == {"2401.00001"}


needs_uvx = pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")


@needs_uvx
def test_arxiv_server_exposes_exactly_the_filtered_tools():
    """Starts the real arXiv MCP server (no API key needed) and checks least-privilege filtering."""

    async def names():
        toolset = _arxiv_toolset()
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    assert asyncio.run(names()) == sorted(ARXIV_TOOLS)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_duo_delegates_over_a2a_with_verified_citations(tmp_path):
    out = tmp_path / "brief.md"
    result = subprocess.run(
        [sys.executable, "-m", "a2a_duo", "speculative decoding for LLM inference", "-o", str(out)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    brief = out.read_text()
    assert "## Key papers" in brief and "arxiv.org/abs/" in brief
    assert "a2a:librarian" in result.stderr
