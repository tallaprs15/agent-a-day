"""Perf Auditor: records a Chrome performance trace for a page and explains its Core Web Vitals.

Tools come from one public MCP server launched over stdio with npx:
  * chrome-devtools-mcp (Google's Chrome DevTools team): opens pages in a headless Chrome, records
    performance traces and returns the DevTools performance insights

The page comes from the user's message (stored in session state by a before_agent callback); nothing about
the subject is hard-coded.
"""

from __future__ import annotations

import json
import os
from typing import Any

from google.adk.agents import Agent
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

from .ledger import RunLedger, extract_url, url_problem

# Pinned so a run today behaves the same as a run next month.
CHROME_DEVTOOLS_SERVER = "chrome-devtools-mcp@1.12.0"

# Least privilege: the server exposes ~35 tools (clicking, typing, JS evaluation, file upload, heap
# snapshots, screenshots, Lighthouse). A read-only audit needs to open one page, optionally throttle it,
# record a trace and drill into insights.
TOOLS = ["new_page", "emulate", "performance_start_trace", "performance_analyze_insight"]

URL_KEY = "target_url"
ASK_FOR_URL = "Which page should I audit? Send a public URL, for example: https://web.dev"

MAX_CPU_THROTTLE = 6
MAX_INSIGHTS = 3

_FORWARD_ENV = ("PATH", "HOME", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")

INSTRUCTION = """\
You are Perf Auditor, a web performance engineer. Audit this page: {target_url}

Use ONLY what your tools return. Process, in order:
1. new_page with the URL above. Note the page id it reports (the number before the page title).
2. Optionally emulate (pageId, cpuThrottlingRate 4, networkConditions "Fast 4G") for a mid-range phone, then
   say in the report that throttling was applied.
3. performance_start_trace with that pageId, reload=true, autoStop=true. The result lists insight sets (use
   their exact ids, e.g. NAVIGATION_0), lab metrics and insight names.
4. performance_analyze_insight for the 1-3 insights most relevant to the slowest metric, using an exact
   insight set id and insight name from the trace result.
Never repeat an identical call. You have a strict tool-call budget.

Write the report in Markdown, exactly these sections:
# Performance audit: <url>
## Core Web Vitals
| Metric | Value | Rating |
|---|---|---|
One row per metric the trace reported, from: LCP, CLS, INP, FCP, TTFB. Value is copied exactly from the tool
result (milliseconds as "315 ms", CLS as "0.00"). Rating is one of Good, Needs improvement, Poor, using
Google's thresholds: LCP 2500/4000 ms, INP 200/500 ms, CLS 0.1/0.25, FCP 1800/3000 ms, TTFB 800/1800 ms
(Good up to the first value, Poor above the second). If a metric was not measured, write "not measured" as
the value and "n/a" as the rating. Lab values are what you measured; field (CrUX) values may be missing.
## Diagnosis
What is slowing the page, citing insight names and the exact millisecond figures they returned.
## Recommendations
A ranked list of at most 5 concrete fixes with the evidence for each. No figures you did not read from a tool.

Rules: Quote times in milliseconds exactly as returned; do not convert units or invent numbers. If a tool
returns an error, say what failed and report only what you measured. A short honest report beats a padded one.
"""


def _chrome_toolset() -> McpToolset:
    args = ["-y", CHROME_DEVTOOLS_SERVER, "--headless", "--isolated", "--no-usage-statistics", "--no-performance-crux"]
    if os.getenv("CHROME_PATH"):
        args.append(f"--executablePath={os.environ['CHROME_PATH']}")
    if os.getenv("CHROME_NO_SANDBOX") == "1":
        args.append("--chromeArg=--no-sandbox")
    env = {k: os.environ[k] for k in _FORWARD_ENV if k in os.environ}
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(command="npx", args=args, env=env),
            timeout=90,  # first run downloads the server
        ),
        tool_filter=TOOLS,
    )


def _text(content) -> str:
    return " ".join(p.text or "" for p in (getattr(content, "parts", None) or []))


def _say(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def _make_resolve_url():
    def resolve_url(callback_context):
        """Runs first on every user message: bind this run to the page the user named."""
        text = _text(callback_context.user_content)
        url = extract_url(text)
        if not url:
            return _say(ASK_FOR_URL)  # ends the turn without spending a model call
        problem = url_problem(url)
        if problem:
            return _say(f"{problem} {ASK_FOR_URL}")
        callback_context.state[URL_KEY] = url
        return None

    return resolve_url


def _make_callbacks(ledger: RunLedger):
    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        # Argument guards run before the budget is spent, so a rejected call is free.
        if tool.name == "new_page":
            problem = url_problem(str(args.get("url", "")))
            if problem:
                return {"error": problem}
            args.pop("isolatedContext", None)
        elif tool.name == "emulate":
            rate = args.get("cpuThrottlingRate")
            if rate is not None:
                args["cpuThrottlingRate"] = max(1, min(float(rate), MAX_CPU_THROTTLE))
            # Only throttling is needed; identity and location overrides are not.
            for key in ("userAgent", "geolocation", "extraHttpHeaders"):
                args.pop(key, None)
        elif tool.name == "performance_start_trace":
            args["reload"] = True
            args["autoStop"] = True
            args.pop("filePath", None)  # never write trace files from a model-chosen path
        elif tool.name == "performance_analyze_insight":
            if ledger.calls_by_tool.get(tool.name, 0) >= MAX_INSIGHTS:
                return {"error": f"At most {MAX_INSIGHTS} insights can be analysed. Write the report now."}
        refusal = ledger.record_tool_call(tool.name)
        if refusal:
            # Returning a dict skips the real call; the model sees this instead.
            return {"error": refusal}
        return None

    def after_tool(tool, args, tool_context, tool_response) -> dict | None:
        ledger.record_evidence(json.dumps(tool_response, default=str, ensure_ascii=False).replace("\\n", "\n"))
        return None

    def after_model(callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage:
            ledger.record_usage(usage.prompt_token_count, usage.candidates_token_count)
        return None

    return before_tool, after_tool, after_model


def build_agent(ledger: RunLedger) -> Agent:
    before_tool, after_tool, after_model = _make_callbacks(ledger)
    # {target_url} is filled from session state by ADK at run time.
    return Agent(
        name="perf_auditor",
        model=ledger.model,
        description="Records a Chrome performance trace for a page and explains its Core Web Vitals.",
        instruction=INSTRUCTION,
        tools=[_chrome_toolset()],
        before_agent_callback=_make_resolve_url(),
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        after_model_callback=after_model,
    )


def default_ledger() -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "10")),
    )


# Entry point for `adk web` / `adk run`: the page comes from each chat message.
root_agent = build_agent(default_ledger())
