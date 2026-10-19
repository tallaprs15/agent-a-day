import asyncio
import os
import shutil
import subprocess
import sys

import pytest
from google.adk.agents import SequentialAgent

from repo_onboarder.agent import (
    DEEPWIKI_TOOLS,
    FETCH_TOOLS,
    MAPPER_TOOLS,
    RESEARCHER_WIKI_TOOLS,
    _deepwiki_toolset,
    _fetch_toolset,
    _make_callbacks,
    _make_pipeline_callbacks,
    build_agent,
)
from repo_onboarder.ledger import RunLedger


class _Tool:
    def __init__(self, name):
        self.name = name


def _ledger(**kw):
    return RunLedger(model="gemini-2.5-flash", repo="o/r", **kw)


def test_before_tool_enforces_budget():
    before_tool, _, _ = _make_callbacks(_ledger(max_tool_calls=1))
    assert before_tool(_Tool("ask_wiki_question"), {"repoName": "o/r"}, None) is None
    assert "budget" in before_tool(_Tool("ask_wiki_question"), {"repoName": "o/r"}, None)["error"]


class _Ctx:
    """Stand-in for ADK's tool/callback context: just session state (plus user message / agent name)."""

    def __init__(self, state=None, text=None, agent_name="repo_onboarder"):
        from google.genai import types

        self.state = dict(state or {})
        self.agent_name = agent_name
        self.user_content = types.Content(role="user", parts=[types.Part(text=text)]) if text else None


def test_before_tool_pins_repo_from_session_state():
    """The repo comes from the session, not from import time: two sessions, two repos."""
    before_tool, _, _ = _make_callbacks(_ledger())
    args = {"repoName": "o/r"}
    assert before_tool(_Tool("ask_wiki_question"), args, _Ctx({"repo_name": "tallaprs15/flickrole"})) is None
    assert args["repoName"] == "tallaprs15/flickrole"


def test_resolve_repo_binds_each_message_to_its_repo():
    ledger = _ledger()
    resolve, _ = _make_pipeline_callbacks(ledger)
    ctx = _Ctx(text="can you fetch the details for tallaprs15/flickrole")
    assert resolve(ctx) is None
    assert ctx.state["repo_name"] == "tallaprs15/flickrole" and ledger.repo == "tallaprs15/flickrole"
    ctx2 = _Ctx(state=ctx.state, text="now https://github.com/psf/requests please")
    assert resolve(ctx2) is None and ctx2.state["repo_name"] == "psf/requests"


def test_resolve_repo_asks_when_no_repo_named():
    resolve, _ = _make_pipeline_callbacks(_ledger())
    reply = resolve(_Ctx(text="hello there"))
    assert "owner/name" in reply.parts[0].text


def test_private_or_unindexed_repo_short_circuits_with_explanation():
    ledger = _ledger()
    _, after_tool, _ = _make_callbacks(ledger)
    _, skip = _make_pipeline_callbacks(ledger)
    ctx = _Ctx({"repo_name": "tallaprs15/flickrole"})
    after_tool(_Tool("read_wiki_structure"), {}, ctx, {"content": [{"text": "Error fetching wiki: Repository not found."}], "isError": True})
    assert "private" in ctx.state["repo_unavailable"]
    assert skip(_Ctx(ctx.state, agent_name="researcher")).parts[0].text.startswith("(skipped")
    assert skip(_Ctx(ctx.state, agent_name="writer")).parts[0].text.startswith("DeepWiki has no wiki for")
    assert skip(_Ctx({"repo_name": "psf/requests", "repo_unavailable": ""}, agent_name="writer")) is None


def test_before_tool_pins_repo_name():
    before_tool, _, _ = _make_callbacks(_ledger())
    args = {"repoName": "someone/else", "question": "q"}
    assert before_tool(_Tool("ask_wiki_question"), args, None) is None
    assert args["repoName"] == "o/r"
    omitted = {"question": "q"}
    before_tool(_Tool("ask_wiki_question"), omitted, None)
    assert omitted["repoName"] == "o/r"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/o/r/README.md",
        "https://raw.githubusercontent.com/other/repo/HEAD/CONTRIBUTING.md",
        "file:///etc/passwd",
        "",
    ],
)
def test_fetch_is_limited_to_the_target_repo(url):
    ledger = _ledger()
    before_tool, _, _ = _make_callbacks(ledger)
    result = before_tool(_Tool("fetch"), {"url": url}, None)
    assert "limited" in result["error"]
    assert ledger.tool_calls == 0  # rejected calls don't spend budget


def test_fetch_allowed_for_target_repo_and_capped():
    before_tool, _, _ = _make_callbacks(_ledger())
    args = {"url": "https://raw.githubusercontent.com/o/r/HEAD/CONTRIBUTING.md", "max_length": 99_999}
    assert before_tool(_Tool("fetch"), args, None) is None
    assert args["max_length"] == 6000


def test_pipeline_is_sequential_with_state_handoff():
    agent = build_agent(_ledger())
    assert isinstance(agent, SequentialAgent)
    assert [a.name for a in agent.sub_agents] == ["mapper", "researcher", "writer"]
    assert [a.output_key for a in agent.sub_agents] == ["outline", "research", "guide"]
    assert agent.sub_agents[2].tools == []  # the writer has no tools: it can only use the digest
    assert "{outline}" in agent.sub_agents[1].instruction and "{research}" in agent.sub_agents[2].instruction


needs_uvx = pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")


def _tool_names(toolset):
    async def names():
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    return asyncio.run(names())


def test_deepwiki_exposes_exactly_the_filtered_tools():
    """Calls the real remote DeepWiki server (no API key needed)."""
    assert _tool_names(_deepwiki_toolset(DEEPWIKI_TOOLS)) == sorted(DEEPWIKI_TOOLS)
    assert _tool_names(_deepwiki_toolset(MAPPER_TOOLS)) == sorted(MAPPER_TOOLS)
    assert _tool_names(_deepwiki_toolset(RESEARCHER_WIKI_TOOLS)) == sorted(RESEARCHER_WIKI_TOOLS)


@needs_uvx
def test_fetch_server_exposes_exactly_the_filtered_tools():
    assert _tool_names(_fetch_toolset()) == sorted(FETCH_TOOLS)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_guide_has_sections_and_verified_links(tmp_path):
    out = tmp_path / "guide.md"
    result = subprocess.run(
        [sys.executable, "-m", "repo_onboarder", "modelcontextprotocol/python-sdk", "-o", str(out)],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stderr
    guide = out.read_text()
    assert "## Architecture map" in guide and "## Local setup" in guide
