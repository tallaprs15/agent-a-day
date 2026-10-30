import asyncio
import json
import os
import shutil
import subprocess
import sys

import pytest

from release_notes.agent import (
    ASK_FOR_REPO,
    GIT_TOOLS,
    LOG_TOOL,
    MAPPERS,
    _git_toolset,
    _make_callbacks,
    _reducer_instruction,
)
from release_notes.demo import make_demo_repo
from release_notes.ledger import MAX_COMMITS, RunLedger, extract_text, parse_log

LOG = "Commit history:\n" + "".join(
    f"Commit: {c * 40}\nAuthor: A\nDate: 2026-09-0{n} 10:00:00+00:00\nMessage: change {n}\n\n" for n, c in enumerate("abcdef", 1)
)


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    def __init__(self, text=""):
        from google.genai import types

        self.user_content = types.Content(role="user", parts=[types.Part(text=text)])
        self.state = {}


def _callbacks(tmp_path, **kwargs):
    ledger = RunLedger(model="m", repo=str(tmp_path), **kwargs)
    return (ledger, *_make_callbacks(ledger))


def test_git_log_is_pinned_to_the_repo_and_capped(tmp_path):
    ledger, _, before_tool, *_ = _callbacks(tmp_path, max_tool_calls=5)
    args = {"repo_path": "/etc", "max_count": 5000, "start_timestamp": "2026-10-01"}
    assert before_tool(_Tool(LOG_TOOL), args, None) is None
    assert args["repo_path"] == str(tmp_path) and args["max_count"] == MAX_COMMITS
    defaults = {"max_count": None, "end_timestamp": ""}
    assert before_tool(_Tool(LOG_TOOL), defaults, None) is None
    assert defaults == {"max_count": 30, "repo_path": str(tmp_path)}


def test_git_log_rejects_bad_arguments_for_free(tmp_path):
    ledger, _, before_tool, *_ = _callbacks(tmp_path)
    for bad in ({"start_timestamp": "--output=/tmp/x"}, {"end_timestamp": "yesterday; rm -rf /"}, {"max_count": "many"}):
        assert "error" in before_tool(_Tool(LOG_TOOL), dict(bad), None)
    assert ledger.tool_calls == 0


def test_budget_is_enforced(tmp_path):
    ledger, _, before_tool, *_ = _callbacks(tmp_path, max_tool_calls=1)
    assert before_tool(_Tool(LOG_TOOL), {"max_count": 3}, None) is None
    assert "budget" in before_tool(_Tool(LOG_TOOL), {"max_count": 3}, None)["error"]


def test_after_tool_records_commits_and_fills_the_map_chunks(tmp_path):
    ledger, _, _, after_tool, *_ = _callbacks(tmp_path)
    ctx = _Ctx()
    reply = after_tool(_Tool(LOG_TOOL), {"max_count": 6}, ctx, {"content": [{"text": LOG}]})
    assert reply["commits"] == 6 and len(ledger.commits) == 6
    assert ctx.state["count"] == 6 and ctx.state["range_label"] == "the last 6 commits"
    sizes = [len(ctx.state[f"chunk_{i}"].splitlines()) for i in range(1, MAPPERS + 1)]
    assert sizes == [2, 2, 1, 1]
    _, _, _, fresh_after_tool, *_ = _callbacks(tmp_path)
    assert "error" in fresh_after_tool(_Tool(LOG_TOOL), {"max_count": 6}, _Ctx(), {"content": [{"text": "Commit history:\n"}]})


def test_empty_chunks_skip_the_model_call(tmp_path):
    *_, skip_empty_chunk, _ = _callbacks(tmp_path)
    ctx = _Ctx()
    ctx.state["chunk_3"] = ""
    ctx.state["chunk_1"] = "aaaaaaa 2026-09-01 change"
    assert skip_empty_chunk(3)(ctx) is not None and skip_empty_chunk(1)(ctx) is None


def test_request_comes_from_the_message_not_the_code(tmp_path):
    """Two different repositories in one session bind separately; a greeting or a bad path asks instead."""
    first, second = tmp_path / "one", tmp_path / "two"
    for repo in (first, second):
        (repo / ".git").mkdir(parents=True)
    ledger, resolve_request, *_ = _callbacks(tmp_path)
    a, b = _Ctx(f"Release notes for {first}, last 5 commits"), _Ctx(f"Now {second} since 2026-10-01")
    assert resolve_request(a) is None and resolve_request(b) is None
    assert a.state["repo"] == str(first) and b.state["repo"] == str(second) and ledger.repo == str(second)
    assert "since 2026-10-01" in b.state["request"] and b.state["chunk_1"] == ""
    assert resolve_request(_Ctx("hello")).parts[0].text == ASK_FOR_REPO
    assert "not the root of a git checkout" in resolve_request(_Ctx(f"Notes for {tmp_path / 'nope'}")).parts[0].text


def test_reducer_instruction_lists_every_mapped_commit():
    ctx = _Ctx()
    ctx.state.update(range_label="the last 3 commits", count=3)
    ctx.state["map_1"] = {"entries": [{"sha": "aaaaaaa", "type": "fix", "summary": "Fixed a crash", "breaking": False}]}
    ctx.state["map_2"] = json.dumps({"entries": [{"sha": "bbbbbbb", "type": "breaking", "summary": "Renamed a flag", "breaking": True}]})
    text = _reducer_instruction(ctx)
    assert "aaaaaaa | fix | - | Fixed a crash" in text and "bbbbbbb | breaking | breaking | Renamed a flag" in text
    assert "the last 3 commits (3 commits)" in text and "{" not in text.split("Output format")[0]


needs_uvx = pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")


@needs_uvx
def test_mcp_server_exposes_exactly_the_filtered_tools():
    """Starts the real MCP server (no API key needed) and checks least-privilege filtering."""

    async def names():
        toolset = _git_toolset()
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    assert asyncio.run(names()) == sorted(GIT_TOOLS)


@needs_uvx
def test_real_git_log_output_parses_into_the_demo_commits():
    """Calls the real server's git_log on the demo repo and checks the parser reads what it returns."""
    repo = make_demo_repo()

    async def log():
        toolset = _git_toolset()
        try:
            tool = next(t for t in await toolset.get_tools() if t.name == LOG_TOOL)
            return await tool.run_async(args={"repo_path": str(repo), "max_count": 10}, tool_context=None)
        finally:
            await toolset.close()

    commits = parse_log(extract_text(asyncio.run(log())))
    assert len(commits) == 10 and commits[0]["message"].startswith("update lockfile")
    assert all(len(c["sha"]) == 40 for c in commits)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_demo_notes_cover_every_commit(tmp_path):
    out, metrics = tmp_path / "notes.md", tmp_path / "m.json"
    result = subprocess.run(
        [sys.executable, "-m", "release_notes", "--demo", "-o", str(out), "--metrics", str(metrics)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(metrics.read_text())
    assert data["checks"]["commits"] == 22 and data["checks"]["notes_cover_every_commit"] is True
    assert data["tool_calls"] <= data["tool_budget"] and "## Breaking changes" in out.read_text()
