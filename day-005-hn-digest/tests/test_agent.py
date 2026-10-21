import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from types import SimpleNamespace

import pytest
from google.genai import types

from hn_digest.agent import (
    FETCH_TOOLS,
    MAX_ARTICLE_FETCHES,
    MEMORY_TOOLS,
    _fetch_toolset,
    _make_callbacks,
    _memory_toolset,
)
from hn_digest.ledger import RunLedger, stored_story_ids

FRONT = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30"
ARTICLE = "https://example.com/post"


class _Tool:
    def __init__(self, name):
        self.name = name


def _callbacks(**ledger_kwargs):
    ledger = RunLedger(model="m", **ledger_kwargs)
    resolve, before_tool, after_tool, after_model = _make_callbacks(ledger)
    return ledger, resolve, before_tool, after_tool


def _message(text):
    return SimpleNamespace(user_content=types.Content(role="user", parts=[types.Part(text=text)]), state={})


def test_algolia_requests_are_trimmed_and_capped():
    ledger, _, before_tool, _ = _callbacks()
    args = {"url": FRONT + "&hitsPerPage=500", "max_length": 90_000}
    assert before_tool(_Tool("fetch"), args, None) is None
    assert args["max_length"] == 8000
    assert "hitsPerPage=30" in args["url"] and "attributesToHighlight=none" in args["url"]
    assert "attributesToRetrieve=title,url,points,num_comments,objectID" in args["url"]


def test_fetch_is_not_free_browsing():
    ledger, _, before_tool, _ = _callbacks()
    assert "https" in before_tool(_Tool("fetch"), {"url": "http://hn.algolia.com/api"}, None)["error"]
    assert "article URLs" in before_tool(_Tool("fetch"), {"url": "https://evil.example/x"}, None)["error"]
    assert ledger.tool_calls == 0  # rejected calls are free


def test_article_fetch_only_for_returned_urls_and_limited():
    ledger, _, before_tool, after_tool = _callbacks()
    after_tool(_Tool("fetch"), {}, None, {"content": [{"text": f'{{"url":"{ARTICLE}"}}'}]})
    args = {"url": ARTICLE, "max_length": 50_000}
    assert before_tool(_Tool("fetch"), args, None) is None
    assert args["max_length"] == 3000
    for _ in range(MAX_ARTICLE_FETCHES - 1):
        assert before_tool(_Tool("fetch"), {"url": ARTICLE}, None) is None
    assert "limit" in before_tool(_Tool("fetch"), {"url": ARTICLE}, None)["error"]


def test_memory_writes_must_be_about_returned_stories():
    ledger, _, before_tool, after_tool = _callbacks()
    after_tool(_Tool("fetch"), {}, None, {"content": [{"text": '{"objectID":"4242"}'}]})
    good = {"entities": [{"name": "hn-4242", "entityType": "hn_story", "observations": []}, {"name": "topic-ai"}]}
    assert before_tool(_Tool("create_entities"), good, None) is None
    invented = {"entities": [{"name": "hn-9999", "entityType": "hn_story", "observations": []}]}
    assert "not in any story result" in before_tool(_Tool("create_entities"), invented, None)["error"]
    other = {"entities": [{"name": "secret", "entityType": "x", "observations": []}]}
    assert "must start with" in before_tool(_Tool("create_entities"), other, None)["error"]
    too_many = {"entities": [{"name": "topic-t"}] * 20}
    assert "at most" in before_tool(_Tool("create_entities"), too_many, None)["error"]


def test_budget_is_enforced():
    ledger, _, before_tool, _ = _callbacks(max_tool_calls=1)
    assert before_tool(_Tool("open_nodes"), {"names": ["hn-1"]}, None) is None
    assert "budget" in before_tool(_Tool("open_nodes"), {"names": ["hn-2"]}, None)["error"]


def test_request_comes_from_the_conversation_not_import_time(tmp_path):
    """Two different requests on one agent: each rebinds state and resets the run."""
    ledger, resolve, _, _ = _callbacks(memory_file=tmp_path / "m.jsonl")
    first, second = _message("5 stories about databases"), _message("what is new in rust")
    assert resolve(first) is None
    ledger.record_tool_call("fetch")
    assert resolve(second) is None
    assert first.state["request"] == "5 stories about databases"
    assert second.state["request"] == "what is new in rust"
    assert second.state["today"]
    assert ledger.request == "what is new in rust" and ledger.tool_calls == 0


def test_greeting_gets_a_question_instead_of_a_model_call():
    _, resolve, _, _ = _callbacks()
    reply = resolve(_message("hi"))
    assert "What kind of Hacker News digest" in reply.parts[0].text


needs_uvx = pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")
needs_npx = pytest.mark.skipif(shutil.which("npx") is None, reason="npx not installed")


def _tool_names(toolset):
    async def names():
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    return asyncio.run(names())


@needs_uvx
def test_fetch_server_exposes_exactly_the_filtered_tools():
    """Starts the real server (no API key needed) and checks least-privilege filtering."""
    assert _tool_names(_fetch_toolset()) == sorted(FETCH_TOOLS)


@needs_npx
def test_memory_server_exposes_exactly_the_filtered_tools(tmp_path):
    assert _tool_names(_memory_toolset(tmp_path / "m.jsonl")) == sorted(MEMORY_TOOLS)


@needs_npx
def test_memory_survives_a_server_restart(tmp_path):
    """The point of the agent: a second server process (a later run) sees what the first one saved."""
    path = tmp_path / "graph.jsonl"

    async def call(name, arguments):
        toolset = _memory_toolset(path)
        try:
            tool = next(t for t in await toolset.get_tools() if t.name == name)
            return await tool.run_async(args=arguments, tool_context=None)
        finally:
            await toolset.close()

    entity = {"name": "hn-4242", "entityType": "hn_story", "observations": ["title: Hello"]}
    asyncio.run(call("create_entities", {"entities": [entity]}))
    assert stored_story_ids(path) == {"4242"}
    found = asyncio.run(call("open_nodes", {"names": ["hn-4242", "hn-1"]}))
    assert "hn-4242" in json.dumps(found, default=str) and "hn-1\"" not in json.dumps(found, default=str)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_digest_skips_a_story_memory_already_holds(tmp_path):
    """Seeds memory with the current top front-page story, then checks the digest leaves it out."""
    with urllib.request.urlopen(FRONT + "&attributesToRetrieve=objectID,title&attributesToHighlight=none", timeout=30) as r:
        top = json.load(r)["hits"][0]
    memory = tmp_path / "graph.jsonl"
    memory.write_text(
        json.dumps({"type": "entity", "name": f"hn-{top['objectID']}", "entityType": "hn_story", "observations": [f"title: {top['title']}"]})
        + "\n"
    )
    out, metrics = tmp_path / "digest.md", tmp_path / "m.json"
    result = subprocess.run(
        [sys.executable, "-m", "hn_digest", "top stories", "-o", str(out), "--metrics", str(metrics), "--memory-file", str(memory)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    assert f"id={top['objectID']}" not in out.read_text()
    checks = json.loads(metrics.read_text())["checks"]
    assert checks["known_before"] == 1 and checks["no_repeats"] and checks["memory_saved"]
