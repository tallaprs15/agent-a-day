import asyncio
import json
import os
import shutil
import subprocess
import sys

import pytest

from meeting_planner.agent import ASK_FOR_REQUEST, TIME_TOOLS, _check_args, _make_callbacks, _time_toolset
from meeting_planner.ledger import RunLedger


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    def __init__(self, text):
        from google.genai import types

        self.user_content = types.Content(role="user", parts=[types.Part(text=text)])
        self.state = {}


def test_before_tool_rejects_bad_arguments_without_spending_budget():
    ledger = RunLedger(model="m", max_tool_calls=5)
    before_tool = _make_callbacks(ledger)[1]
    assert "IANA" in before_tool(_Tool("convert_time"), {"source_timezone": "Narnia/Cair", "time": "09:00", "target_timezone": "UTC"}, None)["error"]
    assert "HH:MM" in before_tool(_Tool("convert_time"), {"source_timezone": "UTC", "time": "9am", "target_timezone": "Europe/London"}, None)["error"]
    assert ledger.tool_calls == 0


def test_before_tool_enforces_budget_and_zone_cap_and_records_zones():
    ledger = RunLedger(model="m", max_tool_calls=2)
    before_tool = _make_callbacks(ledger)[1]
    ok = {"source_timezone": "Europe/London", "time": "09:00", "target_timezone": "Australia/Perth"}
    assert before_tool(_Tool("convert_time"), dict(ok), None) is None
    assert ledger.zones_checked == {"Europe/London", "Australia/Perth"}
    assert before_tool(_Tool("get_current_time"), {"timezone": "Asia/Tokyo"}, None) is None
    assert "budget" in before_tool(_Tool("get_current_time"), {"timezone": "Asia/Tokyo"}, None)["error"]

    wide = RunLedger(model="m")
    wide.zones_checked = {f"Etc/GMT+{i}" for i in range(1, 7)}
    assert "At most" in _check_args("get_current_time", {"timezone": "Asia/Tokyo"}, wide)["error"]


def test_request_comes_from_the_message_not_the_code():
    """Two different requests in one session bind to two different states; a greeting asks instead."""
    ledger = RunLedger(model="m")
    resolve_request = _make_callbacks(ledger)[0]
    first, second = _Ctx("30 min, Melbourne and Lisbon"), _Ctx("1 hour, Tokyo and Toronto")
    assert resolve_request(first) is None and resolve_request(second) is None
    assert first.state["request"] == "30 min, Melbourne and Lisbon"
    assert second.state["request"] == "1 hour, Tokyo and Toronto" and ledger.request == second.state["request"]
    assert {"today", "weekday"} <= second.state.keys()
    reply = resolve_request(_Ctx("hello"))
    assert reply.parts[0].text == ASK_FOR_REQUEST


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


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_plan_is_valid_and_verified(tmp_path):
    out, metrics = tmp_path / "plan.md", tmp_path / "m.json"
    result = subprocess.run(
        [sys.executable, "-m", "meeting_planner",
         "A 45-minute sync next week between Melbourne, Singapore and Mumbai, working hours 09:00-17:00",
         "-o", str(out), "--metrics", str(metrics)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    assert "Option 1" in out.read_text()
    assert json.loads(metrics.read_text())["checks"]["verified_by_recompute"] is True
