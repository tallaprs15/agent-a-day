"""Time Desk: the agent under test. Answers time-zone questions using the public time MCP server.

The point of day 015 is not the agent but the harness around it (see cases.py and suite.py): a versioned
eval set scored on tool trajectory and response match with ADK's evaluation framework.
"""

from __future__ import annotations

import os
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.adk.agents import Agent
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

from .ledger import RunLedger

# Pinned so a run today behaves the same as a run next month.
TIME_SERVER = "mcp-server-time==2026.10.28"
# Least privilege: the server only has these two tools; the filter keeps it that way if it grows.
TIME_TOOLS = ["get_current_time", "convert_time"]
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

INSTRUCTION = """\
You are Time Desk, a precise assistant for time-zone questions.

Rules:
- Use IANA zone names (Asia/Tokyo, Europe/London). Map city names to zones yourself.
- To convert a time of day between zones, call convert_time. To say what time it is somewhere, call
  get_current_time. Never do the arithmetic in your head.
- Never assume a zone the user did not name (not UTC, not a default). If the source zone, target zone or time is missing, ask one short
  clarifying question and call no tool. If a tool reports an invalid zone,
  tell the user, do not guess another zone.
- If the question is not about time or time zones, say in one sentence that you only help with time zones.
  Do not call tools for it.
- Answer in one sentence of this shape: "09:00 in Asia/Tokyo is 05:30 in Asia/Kolkata."
- You have a strict tool-call budget. Do not repeat identical calls.
"""


def _server_env() -> dict[str, str]:
    """Environment for the server subprocess: PATH/HOME plus proxy and CA settings when the host needs them."""
    from mcp.client.stdio import get_default_environment

    env = get_default_environment()
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "UV_SYSTEM_CERTS", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                "https_proxy", "http_proxy", "no_proxy"):
        if os.getenv(key):
            env[key] = os.environ[key]
    return env


def _time_toolset() -> McpToolset:
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(command="uvx", args=[TIME_SERVER], env=_server_env()),
            timeout=60,  # first run downloads the server
        ),
        tool_filter=TIME_TOOLS,
    )


def _tool_zones(tool_name: str, args: dict[str, Any]) -> list[str]:
    keys = ["timezone"] if tool_name == "get_current_time" else ["source_timezone", "target_timezone"]
    return [str(args.get(k, "")) for k in keys]


def check_args(tool_name: str, args: dict[str, Any]) -> dict | None:
    """Argument guardrails. Returns an error dict to reject the call (rejected calls cost no budget)."""
    for zone in _tool_zones(tool_name, args):
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return {"error": f"'{zone}' is not an IANA time zone name (for example Europe/London)."}
    if tool_name == "convert_time" and not HHMM.match(str(args.get("time", ""))):
        return {"error": "time must be 24-hour HH:MM."}
    return None


def make_callbacks(ledger: RunLedger):
    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        rejected = check_args(tool.name, args)
        if rejected:
            return rejected
        if not ledger.record_tool_call(tool.name):
            # Returning a dict skips the real call; the model sees this instead.
            return {"error": f"Tool budget of {ledger.max_tool_calls} calls reached. Answer now."}
        return None

    def after_model(callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage:
            ledger.record_usage(usage.prompt_token_count, usage.candidates_token_count)
        return None

    return before_tool, after_model


def build_agent(ledger: RunLedger) -> Agent:
    before_tool, after_model = make_callbacks(ledger)
    return Agent(
        name="time_desk",
        model=ledger.model,
        description="Answers time-zone questions using the time MCP server.",
        instruction=INSTRUCTION,
        tools=[_time_toolset()],
        # Temperature 0: an eval is only useful if a score change means the agent changed, not the dice.
        generate_content_config=types.GenerateContentConfig(temperature=0.0),
        before_tool_callback=before_tool,
        after_model_callback=after_model,
    )


def default_ledger() -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "30")),
    )


# Entry point for `adk web`, `adk run` and `adk eval`; the question comes from each chat message.
LEDGER = default_ledger()
root_agent = build_agent(LEDGER)
