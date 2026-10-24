import asyncio
import os
import shutil
import subprocess
import sys

import pytest
from google.adk.agents import ParallelAgent, SequentialAgent
from google.genai import types

from aws_architect.agent import (
    ASK_FOR_ARCHITECTURE,
    DOC_TOOLS,
    PILLARS,
    _doc_toolset,
    _make_callbacks,
    _make_pipeline_callbacks,
    build_agent,
)
from aws_architect.ledger import RunLedger

DOC = "https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingEncryption.html"


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    """Stand-in for ADK's tool/callback context: session state, user message and the calling agent."""

    def __init__(self, text=None, agent_name="cost_reviewer"):
        self.state = {}
        self.agent_name = agent_name
        self.user_content = types.Content(role="user", parts=[types.Part(text=text)]) if text else None


def _ledger(**kw):
    return RunLedger(model="gemini-2.5-flash", **kw)


def test_before_tool_enforces_per_reviewer_budget():
    before_tool, _, _ = _make_callbacks(_ledger(max_tool_calls=10, max_calls_per_reviewer=1))
    args = {"search_phrase": "x"}
    assert before_tool(_Tool("search_documentation"), dict(args), _Ctx(agent_name="cost_reviewer")) is None
    assert "budget" in before_tool(_Tool("search_documentation"), dict(args), _Ctx(agent_name="cost_reviewer"))["error"]
    assert before_tool(_Tool("search_documentation"), dict(args), _Ctx(agent_name="security_reviewer")) is None


def test_before_tool_caps_search_arguments():
    before_tool, _, _ = _make_callbacks(_ledger())
    args = {"search_phrase": "x" * 1000, "limit": 50}
    assert before_tool(_Tool("search_documentation"), args, _Ctx()) is None
    assert len(args["search_phrase"]) == 200 and args["limit"] == 5


def test_read_sections_is_limited_to_aws_docs_and_three_titles():
    ledger = _ledger()
    before_tool, _, _ = _make_callbacks(ledger)
    blocked = before_tool(_Tool("read_sections"), {"url": "https://evil.example.com/x", "section_titles": ["a"]}, _Ctx())
    assert "limited" in blocked["error"] and ledger.tool_calls == 0  # rejected calls are free
    args = {"url": DOC, "section_titles": list("abcdef")}
    assert before_tool(_Tool("read_sections"), args, _Ctx()) is None
    assert args["section_titles"] == ["a", "b", "c"]


def test_after_tool_records_returned_urls():
    ledger = _ledger()
    _, after_tool, _ = _make_callbacks(ledger)
    after_tool(_Tool("search_documentation"), {}, _Ctx(), {"content": [{"text": f'{{"url": "{DOC}"}}'}]})
    assert DOC in ledger.seen_urls


def test_architecture_comes_from_each_message_not_import_time():
    """Two different architectures in two turns of one session each bind to state."""
    resolve = _make_pipeline_callbacks(_ledger())
    ctx = _Ctx(text="An API Gateway fronting Lambda functions that write to DynamoDB.")
    assert resolve(ctx) is None and "DynamoDB" in ctx.state["architecture"]
    ctx2 = _Ctx(text="A single EC2 instance running MySQL with nightly EBS snapshots.")
    ctx2.state = ctx.state
    assert resolve(ctx2) is None and "EC2" in ctx2.state["architecture"] and "DynamoDB" not in ctx2.state["architecture"]


def test_missing_architecture_triggers_a_clarifying_question():
    resolve = _make_pipeline_callbacks(_ledger())
    assert resolve(_Ctx(text="hi")).parts[0].text == ASK_FOR_ARCHITECTURE
    assert resolve(_Ctx(text=None)).parts[0].text == ASK_FOR_ARCHITECTURE


def test_pipeline_shape_is_parallel_fan_out_then_merge():
    root = build_agent(_ledger())
    assert isinstance(root, SequentialAgent)
    fan_out, merger = root.sub_agents
    assert isinstance(fan_out, ParallelAgent) and merger.name == "merger"
    assert [a.name for a in fan_out.sub_agents] == list(PILLARS)
    # every reviewer writes to its own state key, so parallel branches never collide
    assert len({a.output_key for a in fan_out.sub_agents}) == 3
    assert not merger.tools  # the merger can only use what the reviewers found


needs_uvx = pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")


@needs_uvx
def test_mcp_server_exposes_exactly_the_filtered_tools():
    """Starts the real AWS Documentation MCP server (no API key needed) and checks least-privilege filtering."""

    async def names():
        toolset = _doc_toolset()
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    assert asyncio.run(names()) == sorted(DOC_TOOLS)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_review_has_all_sections_and_verified_links(tmp_path):
    out = tmp_path / "review.md"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "aws_architect",
            "A public web app: CloudFront in front of an ALB, ECS Fargate tasks in two AZs, an RDS PostgreSQL instance, "
            "and an S3 bucket for user uploads.",
            "-o",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    report = out.read_text()
    assert "## Top priorities" in report and "docs.aws.amazon.com" in report
