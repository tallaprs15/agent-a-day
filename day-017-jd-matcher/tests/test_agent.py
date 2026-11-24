import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from google.adk.agents import SequentialAgent
from google.genai import types

from jd_matcher.agent import (
    FETCH_TOOLS,
    FILESYSTEM_TOOLS,
    Analysis,
    _filesystem_toolset,
    _fetch_toolset,
    _make_render,
    _make_resolve_request,
    _make_tool_callbacks,
    build_agent,
)
from jd_matcher.ledger import RunLedger

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    def __init__(self, user_text="", state=None):
        self.state = state if state is not None else {}
        self.user_content = types.Content(role="user", parts=[types.Part(text=user_text)])


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch):
    monkeypatch.setenv("JD_SANDBOX", str(SAMPLES))


def _state(cv="cv.md", job="job.md"):
    return {"cv_ref": cv, "job_ref": job}


def test_before_tool_enforces_budget():
    ledger = RunLedger(model="gemini-3.5-flash-lite", max_tool_calls=1)
    before_tool, _, _ = _make_tool_callbacks(ledger)
    ctx = _Ctx(state=_state())
    assert before_tool(_Tool("read_text_file"), {"path": "cv.md"}, ctx) is None
    assert "budget" in before_tool(_Tool("read_text_file"), {"path": "job.md"}, ctx)["error"]


def test_before_tool_only_reads_the_named_files_inside_the_sandbox():
    before_tool, _, _ = _make_tool_callbacks(RunLedger(model="m"))
    ctx = _Ctx(state=_state())
    args = {"path": "cv.md", "tail": 5, "head": 99999}
    assert before_tool(_Tool("read_text_file"), args, ctx) is None
    assert args["path"] == str(SAMPLES / "cv.md") and "tail" not in args and args["head"] == 400
    for bad in ("../pyproject.toml", "job-frontend.md", "/etc/hostname", "notes.json"):
        assert "error" in before_tool(_Tool("read_text_file"), {"path": bad}, ctx), bad


def test_before_tool_fetches_only_the_named_url_and_caps_length():
    before_tool, _, _ = _make_tool_callbacks(RunLedger(model="m"))
    ctx = _Ctx(state=_state(job="https://example.com/jobs/1"))
    args = {"url": "https://example.com/jobs/1", "max_length": 500_000, "raw": True}
    assert before_tool(_Tool("fetch"), args, ctx) is None
    assert args["max_length"] == 12_000 and args["raw"] is False
    assert "error" in before_tool(_Tool("fetch"), {"url": "https://evil.example/x"}, ctx)


def test_after_tool_stores_documents_in_state_not_in_model_memory():
    _, after_tool, _ = _make_tool_callbacks(RunLedger(model="m"))
    ctx = _Ctx(state=_state())
    after_tool(_Tool("read_text_file"), {"path": str(SAMPLES / "cv.md")}, ctx, {"content": [{"type": "text", "text": "CV TEXT"}]})
    after_tool(_Tool("read_text_file"), {"path": str(SAMPLES / "job.md")}, ctx, {"content": [{"type": "text", "text": "JOB TEXT"}]})
    assert (ctx.state["cv_text"], ctx.state["jd_text"]) == ("CV TEXT", "JOB TEXT")
    ctx2 = _Ctx(state=_state(job="https://example.com/j"))
    after_tool(_Tool("fetch"), {"url": "https://example.com/j"}, ctx2, "Contents of https://example.com/j:\nJOB")
    assert ctx2.state["jd_text"].endswith("JOB") and "cv_text" not in ctx2.state


def test_inputs_come_from_each_message_not_import_time():
    resolve = _make_resolve_request()
    first, second = _Ctx("CV: cv.md  Job: job.md"), _Ctx("CV: cv.md  Job: job-frontend.md")
    assert resolve(first) is None and resolve(second) is None
    assert first.state["job_ref"] == "job.md" and second.state["job_ref"] == "job-frontend.md"
    # same session, new message: state is rebound and stale documents are cleared
    shared = _Ctx("CV: cv.md  Job: job.md", state={"cv_text": "old", "jd_text": "old"})
    assert resolve(shared) is None and shared.state["cv_text"] == ""


def test_missing_or_unsafe_inputs_trigger_a_clarifying_question():
    resolve = _make_resolve_request()
    reply = resolve(_Ctx("how well do I fit?"))
    assert reply is not None and "which CV" in reply.parts[0].text
    assert resolve(_Ctx("CV: ../secrets.md  Job: job.md")) is not None


def test_render_verifies_quotes_and_replaces_the_models_json():
    ledger = RunLedger(model="m")
    analysis = {
        "role_title": "Data Platform Engineer",
        "fit_summary": "Good.",
        "requirements": [
            {"requirement": "Airflow", "jd_quote": "workflow orchestration tools such as Airflow or Dagster", "importance": "must",
             "status": "met", "cv_evidence": "Wrote Airflow pipelines in Python"},
            {"requirement": "Kafka", "jd_quote": "streaming pipelines with Kafka or similar", "importance": "must",
             "status": "met", "cv_evidence": "Ran Kafka clusters at scale"},  # invented
        ],
        "tailored_bullets": [],
    }
    state = {"analysis": json.dumps(analysis), "cv_text": (SAMPLES / "cv.md").read_text(), "jd_text": (SAMPLES / "job.md").read_text()}
    out = _make_render(ledger)(_Ctx(state=state))
    assert "Fit score" in out.parts[0].text
    assert ledger.score == 50.0 and ledger.counts["gap"] == 1 and len(ledger.problems) == 1


def test_pipeline_shape_and_schema():
    root = build_agent(RunLedger(model="gemini-3.5-flash-lite"))
    assert isinstance(root, SequentialAgent)
    assert [a.name for a in root.sub_agents] == ["reader", "matcher"]
    assert root.sub_agents[1].output_schema is Analysis and not root.sub_agents[1].tools


needs_servers = pytest.mark.skipif(shutil.which("uvx") is None or shutil.which("npx") is None, reason="needs uvx and npx")


@needs_servers
@pytest.mark.parametrize("factory,expected", [(_filesystem_toolset, FILESYSTEM_TOOLS), (_fetch_toolset, FETCH_TOOLS)])
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
def test_live_report_has_only_verified_quotes(tmp_path):
    out, metrics = tmp_path / "report.md", tmp_path / "m.json"
    result = subprocess.run(
        [sys.executable, "-m", "jd_matcher", "CV: cv.md  Job: job.md", "-o", str(out), "--metrics", str(metrics)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    report = out.read_text()
    assert "## Requirements" in report and "Fit score" in report
    assert json.loads(metrics.read_text())["passed"] is True
