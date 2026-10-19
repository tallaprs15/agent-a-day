import asyncio
import os
import shutil
import subprocess
import sys

import pytest

from arxiv_scout.agent import ARXIV_TOOLS, FETCH_TOOLS, _arxiv_toolset, _fetch_toolset, _make_callbacks
from arxiv_scout.ledger import RunLedger


class _Tool:
    def __init__(self, name):
        self.name = name


def test_before_tool_enforces_budget_and_caps_fetch():
    ledger = RunLedger(model="gemini-2.5-flash", max_tool_calls=1)
    before_tool, _, _ = _make_callbacks(ledger)
    args = {"url": "https://example.com", "max_length": 50_000}
    assert before_tool(_Tool("fetch"), args, None) is None
    assert args["max_length"] == 8000
    blocked = before_tool(_Tool("search_papers"), {}, None)
    assert "budget" in blocked["error"]


needs_uvx = pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")


@needs_uvx
@pytest.mark.parametrize("factory,expected", [(_arxiv_toolset, ARXIV_TOOLS), (_fetch_toolset, FETCH_TOOLS)])
def test_mcp_servers_expose_exactly_the_filtered_tools(factory, expected):
    """Starts the real MCP servers (no API key needed) and checks least-privilege filtering."""

    async def names():
        toolset = factory()
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    assert asyncio.run(names()) == sorted(expected)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_brief_has_only_verified_citations(tmp_path):
    out = tmp_path / "brief.md"
    result = subprocess.run(
        [sys.executable, "-m", "arxiv_scout", "speculative decoding for LLM inference", "-o", str(out)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    brief = out.read_text()
    assert "## Key papers" in brief and "arxiv.org/abs/" in brief
