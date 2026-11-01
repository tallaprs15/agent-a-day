import asyncio
import json
import os
import shutil
import subprocess
import sys

import pytest

from agent_evals.agent import TIME_TOOLS, _time_toolset, check_args, make_callbacks
from agent_evals.ledger import RunLedger


class _Tool:
    def __init__(self, name):
        self.name = name


def test_check_args_rejects_unknown_zone_and_bad_time():
    assert "IANA" in check_args("convert_time", {"source_timezone": "Narnia/Cair", "time": "09:00", "target_timezone": "UTC"})["error"]
    assert "HH:MM" in check_args("convert_time", {"source_timezone": "UTC", "time": "9am", "target_timezone": "Asia/Tokyo"})["error"]
    assert check_args("get_current_time", {"timezone": "Asia/Tokyo"}) is None


def test_before_tool_rejections_are_free_and_budget_is_enforced():
    ledger = RunLedger(model="m", max_tool_calls=2)
    before_tool, _ = make_callbacks(ledger)
    assert before_tool(_Tool("get_current_time"), {"timezone": "Nope/Zone"}, None)["error"]
    assert ledger.tool_calls == 0
    assert before_tool(_Tool("get_current_time"), {"timezone": "Asia/Tokyo"}, None) is None
    assert before_tool(_Tool("get_current_time"), {"timezone": "Asia/Tokyo"}, None) is None
    assert "budget" in before_tool(_Tool("get_current_time"), {"timezone": "Asia/Tokyo"}, None)["error"]
    assert ledger.calls_by_tool == {"get_current_time": 2}


def test_after_model_accumulates_tokens_across_cases():
    ledger = RunLedger(model="gemini-2.5-flash-lite")
    _, after_model = make_callbacks(ledger)
    usage = type("U", (), {"prompt_token_count": 100, "candidates_token_count": 10})()
    for _ in range(2):
        after_model(None, type("R", (), {"usage_metadata": usage})())
    assert (ledger.model_calls, ledger.prompt_tokens, ledger.output_tokens) == (2, 200, 20)
    assert ledger.cost_usd() == pytest.approx((200 * 0.10 + 20 * 0.40) / 1e6)


def test_agent_has_no_hardcoded_task_and_is_deterministic():
    from agent_evals.agent import root_agent

    assert "{" not in root_agent.instruction  # the question comes from the user's message, never from the code
    assert root_agent.generate_content_config.temperature == 0.0


needs_uvx = pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")


@needs_uvx
def test_time_server_exposes_exactly_the_filtered_tools():
    """Starts the real MCP server (no API key needed) and checks least-privilege filtering."""

    async def names():
        toolset = _time_toolset()
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    assert asyncio.run(names()) == sorted(TIME_TOOLS)


@needs_uvx
@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="GOOGLE_API_KEY not set")
def test_live_eval_suite_meets_thresholds(tmp_path):
    """End to end: the CLI runs the whole eval set against Gemini and exits 0 only if every threshold holds."""
    metrics = tmp_path / "m.json"
    proc = subprocess.run(
        [sys.executable, "-W", "ignore", "-m", "agent_evals", "-o", str(tmp_path / "r.md"), "--metrics", str(metrics)],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    data = json.loads(metrics.read_text())
    assert data["passed"] and data["tool_calls"] <= data["tool_budget"]
    assert data["checks"]["scores"]["tool_trajectory_avg_score"] == 1.0
