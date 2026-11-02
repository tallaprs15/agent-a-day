import asyncio
import json
import os
import subprocess
import sys

import pytest
from google.adk.agents import SequentialAgent
from google.genai import types

from kg_builder.agent import (
    ASK_FOR_URLS,
    FETCH_SERVER,
    FETCH_TOOLS,
    MAX_PAGE_CHARS,
    MEMORY_SERVER,
    MEMORY_TOOLS,
    _fetch_toolset,
    _make_after_extractor,
    _make_before_extractor,
    _make_before_writer,
    _make_report,
    _make_resolve_request,
    _make_tool_callbacks,
    _memory_toolset,
    build_agent,
)
from kg_builder.ledger import RunLedger

URL_A, URL_B = "https://example.com/a", "https://example.com/b"
QUOTE = "Ada Lovelace worked with Charles Babbage on the Analytical Engine."


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    """Stand-in for ADK's callback context: session state and the user message."""

    def __init__(self, text=None, state=None):
        self.state = state if state is not None else {}
        self.user_content = types.Content(role="user", parts=[types.Part(text=text)]) if text else None


def _ledger(**kw):
    ledger = RunLedger(model="gemini-2.5-flash", **kw)
    ledger.urls = [URL_A, URL_B]
    return ledger


def test_urls_come_from_each_message_not_import_time():
    ledger = _ledger()
    resolve = _make_resolve_request(ledger)
    ctx = _Ctx("please graph https://one.example/x and https://two.example/y")
    assert resolve(ctx) is None and ctx.state["urls"] == "https://one.example/x\nhttps://two.example/y"
    ctx2 = _Ctx("now https://three.example/z")  # same session, different input
    assert resolve(ctx2) is None and ctx2.state["urls"] == "https://three.example/z"
    assert ledger.urls == ["https://three.example/z"]


def test_missing_links_trigger_a_clarifying_question():
    resolve = _make_resolve_request(_ledger())
    for text in (None, "build me a graph", "http://not-secure.example"):
        reply = resolve(_Ctx(text))
        assert reply.parts[0].text == ASK_FOR_URLS


def test_fetch_is_limited_to_the_users_urls_once_each_with_capped_size():
    ledger = _ledger()
    before_tool, after_tool, _ = _make_tool_callbacks(ledger)
    assert "user gave" in before_tool(_Tool("fetch"), {"url": "https://evil.example/"}, _Ctx())["error"]
    args = {"url": URL_A, "max_length": 10**9, "start_index": 500, "raw": True}
    assert before_tool(_Tool("fetch"), args, _Ctx()) is None
    assert args["max_length"] == MAX_PAGE_CHARS and args["start_index"] == 0 and args["raw"] is False
    after_tool(_Tool("fetch"), {"url": URL_A}, _Ctx(), {"content": [{"type": "text", "text": "page text"}]})
    assert ledger.pages == {URL_A: "page text"} and ledger.fetched == {URL_A}
    assert "already fetched" in before_tool(_Tool("fetch"), {"url": URL_A}, _Ctx())["error"]


def test_a_failed_fetch_is_not_marked_fetched():
    ledger = _ledger()
    before_tool, _, _ = _make_tool_callbacks(ledger)
    assert before_tool(_Tool("fetch"), {"url": URL_A}, _Ctx()) is None  # the tool then fails: after_tool never runs
    assert ledger.fetched == set()


def test_before_tool_enforces_budget():
    ledger = _ledger(max_tool_calls=1)
    before_tool, _, _ = _make_tool_callbacks(ledger)
    assert before_tool(_Tool("fetch"), {"url": URL_A}, _Ctx()) is None
    assert "budget" in before_tool(_Tool("fetch"), {"url": URL_B}, _Ctx())["error"]


def _planned_ledger():
    ledger = _ledger()
    ledger.pages = {URL_A: QUOTE}
    ledger.fetched = {URL_A}
    ledger.validate(
        {
            "entities": [
                {"name": "Ada Lovelace", "type": "person", "description": "d", "quote": QUOTE, "source": URL_A},
                {"name": "Charles Babbage", "type": "person", "description": "d", "quote": QUOTE, "source": URL_A},
            ],
            "relations": [
                {"source_entity": "Ada Lovelace", "target_entity": "Charles Babbage", "relation": "worked_with", "quote": QUOTE, "source": URL_A}
            ],
        }
    )
    return ledger


def test_memory_writes_are_reduced_to_the_validated_plan():
    ledger = _planned_ledger()
    before_tool, _, _ = _make_tool_callbacks(ledger)
    args = {"entities": [{"name": "Ada Lovelace", "entityType": "x", "observations": ["lies"]}, {"name": "Grace Hopper"}]}
    assert before_tool(_Tool("create_entities"), args, _Ctx()) is None
    assert [e["name"] for e in args["entities"]] == ["Ada Lovelace"] and args["entities"][0]["entityType"] == "person"
    assert "in the validated plan" in before_tool(_Tool("create_entities"), {"entities": [{"name": "Grace Hopper"}]}, _Ctx())["error"]
    rel = {"relations": [{"from": "Ada Lovelace", "to": "Charles Babbage", "relationType": "worked_with"}, {"from": "Ada Lovelace", "to": "Charles Babbage", "relationType": "loved"}]}
    assert before_tool(_Tool("create_relations"), rel, _Ctx()) is None and len(rel["relations"]) == 1
    names = {"names": [f"n{i}" for i in range(100)]}
    assert before_tool(_Tool("open_nodes"), names, _Ctx()) is None and len(names["names"]) == 30


def test_extractor_callback_validates_and_publishes_the_plan():
    ledger = _ledger()
    ledger.pages = {URL_A: QUOTE}
    ctx = _Ctx(state={"extraction": {"entities": [
        {"name": "Ada Lovelace", "type": "person", "description": "d", "quote": QUOTE, "source": URL_A},
        {"name": "Grace Hopper", "type": "person", "description": "d", "quote": "Grace Hopper wrote a compiler.", "source": URL_A},
    ]}})
    assert _make_after_extractor(ledger)(ctx) is None
    plan = json.loads(ctx.state["plan"])
    assert [e["name"] for e in plan["entities"]] == ["Ada Lovelace"] and plan["relations"] == []
    assert len(ledger.dropped) == 1


def test_pipeline_stops_early_when_there_is_nothing_to_do():
    ledger = _ledger()
    assert "None of the pages could be fetched" in _make_before_extractor(ledger)(_Ctx()).parts[0].text
    assert "nothing was written" in _make_before_writer(ledger)(_Ctx()).parts[0].text
    ledger2 = _planned_ledger()
    assert _make_before_extractor(ledger2)(_Ctx()) is None and _make_before_writer(ledger2)(_Ctx()) is None
    assert "Knowledge graph from 1 source" in _make_report(ledger2)(_Ctx()).parts[0].text


def test_pipeline_shape_is_fetch_then_extract_then_write(tmp_path):
    agent = build_agent(_ledger(), memory_path=tmp_path / "m.jsonl")
    assert isinstance(agent, SequentialAgent)
    assert [a.name for a in agent.sub_agents] == ["fetcher", "extractor", "writer"]
    fetcher, extractor, writer = agent.sub_agents
    assert extractor.output_schema is not None and not extractor.tools  # the extractor has no tools
    assert extractor.include_contents == "none" and writer.include_contents == "none"
    assert len(fetcher.tools) == 1 and len(writer.tools) == 1


def test_pinned_versions_and_filters():
    assert "==" in FETCH_SERVER and MEMORY_SERVER.rsplit("@", 1)[1][0].isdigit()
    assert FETCH_TOOLS == ["fetch"] and MEMORY_TOOLS == ["create_entities", "create_relations", "open_nodes"]


def _names(toolset):
    async def go():
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    return asyncio.run(go())


def test_fetch_server_exposes_exactly_the_filtered_tools():
    """Starts the real mcp-server-fetch over stdio and checks least-privilege filtering."""
    assert _names(_fetch_toolset()) == ["fetch"]


def test_memory_server_exposes_exactly_the_filtered_tools(tmp_path):
    """Starts the real memory server (it exposes 9 tools) and checks only 3 reach the agent."""
    assert _names(_memory_toolset(tmp_path / "memory.jsonl")) == sorted(MEMORY_TOOLS)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_graph_is_grounded_written_and_read_back(tmp_path):
    out, metrics, log = tmp_path / "report.md", tmp_path / "m.json", tmp_path / "run.log"
    env = {**os.environ, "KG_MEMORY_FILE": str(tmp_path / "memory.jsonl")}
    result = subprocess.run(
        [
            sys.executable, "-m", "kg_builder",
            "https://peps.python.org/pep-0484/", "https://peps.python.org/pep-0526/",
            "-o", str(out), "--metrics", str(metrics),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    log.write_text(result.stderr)
    assert result.returncode == 0, result.stderr[-2000:]
    m = json.loads(metrics.read_text())
    assert m["passed"] and m["checks"]["entities_persisted"] and m["checks"]["relations_persisted"]
    assert m["checks"]["entities"] >= 3 and m["checks"]["relations"] >= 1
    assert "```mermaid" in out.read_text()
    assert "read_back" in m["checks"] and (tmp_path / "memory.jsonl").read_text().count('"type":"entity"') >= 3
