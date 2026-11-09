"""A11y Auditor: audits a page's accessibility in a real browser against a fixed WCAG rubric.

One agent drives Playwright MCP over stdio, read-only: it can load pages, read their accessibility
snapshot and press Tab to see where keyboard focus goes. It cannot click, type or run scripts.
The rubric (see rubric.py) is data: the agent must grade every criterion, cite a quote from a tool
result for each failure, and the post-run checks reject a report that skips a criterion, invents
evidence or contradicts its own scorecard.

  * Playwright MCP (Microsoft, stdio via npx): browser automation through accessibility snapshots
"""

from __future__ import annotations

import copy
import os
import tempfile
from typing import Any

from google.adk.agents import Agent
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

from .ledger import RunLedger, target_from_message
from .rubric import in_scope, rubric_table

# Pinned so a run today behaves the same as a run next month.
PLAYWRIGHT_SERVER = "@playwright/mcp@0.0.85"

# Least privilege: the server exposes ~30 tools, including arbitrary JavaScript (browser_evaluate),
# clicking, typing, file upload and downloads. An audit only needs to load, read and tab.
PLAYWRIGHT_TOOLS = ["browser_navigate", "browser_snapshot", "browser_press_key"]
ALLOWED_KEYS = {"Tab", "Shift+Tab"}

TARGET_KEY = "target_url"
ASK_FOR_TARGET = (
    "Which page should I audit? Send a URL, e.g. \"https://example.com/\"."
)

URL_MAX_CHARS = 500
TOOL_RESULT_MAX_CHARS = 8000  # per text part; keeps one big snapshot from dominating the context

INSTRUCTION = """\
You are A11y Auditor. Audit the page below for accessibility using ONLY what your tools show you.

Target: {target_url}

Rubric (grade every criterion, no others):
""" + rubric_table() + """

Process:
1. browser_navigate to the target, then browser_snapshot. The snapshot is the accessibility tree
   (roles, accessible names, heading levels, links with their /url); it also shows the page title when
   the page has one. browser_navigate alone does not return the tree.
2. Press Tab with browser_press_key up to 4 times, then browser_snapshot again, to see where keyboard
   focus goes (the focused element is marked [active]).
3. You may audit up to 2 more pages on the same site if a link on the target suggests one is worth it.
   Do not leave the site. Use at most 12 tool calls and never repeat an identical call.
4. Write the report.

Rules:
- Grade each criterion Pass, Fail or Not assessed. Use Not assessed when the snapshot cannot show it
  (for example colour contrast or page language are out of scope). Never guess.
- A Fail needs a finding. Every finding needs an Evidence line with at least one short quote, in
  backticks, copied exactly from a tool result (for example `img [ref=e3]` or `button [ref=e7]`).
- Only mention URLs a tool returned.

Output format (Markdown), with these exact headings:
## Summary      (2-3 sentences: what you audited and the headline result)
## Scorecard
| WCAG | Criterion | Result | Findings |
|---|---|---|---|
(one row per rubric criterion, in rubric order; Findings lists ids like A11Y-1, or "-")
## Findings
### A11Y-1 · <short title>
- **WCAG:** <criterion id, for example 1.1.1>
- **Severity:** critical | serious | moderate | minor
- **Where:** <page URL and element>
- **Evidence:** `exact quote from a tool result`
- **Fix:** <one concrete change>
(repeat for each finding; write "No failures found." if there are none)
"""


def _server_env() -> dict[str, str]:
    """Environment for the npx subprocess: PATH/HOME plus proxy, CA and browser-path settings."""
    from mcp.client.stdio import get_default_environment

    env = get_default_environment()
    for key in ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy",
                "http_proxy", "no_proxy", "PLAYWRIGHT_BROWSERS_PATH", "PLAYWRIGHT_MCP_EXECUTABLE_PATH"):
        if os.getenv(key):
            env[key] = os.environ[key]
    return env


def _server_args() -> list[str]:
    args = ["-y", PLAYWRIGHT_SERVER, "--headless", "--isolated", "--output-dir", tempfile.mkdtemp(prefix="a11y-")]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")  # Chromium refuses to start its sandbox as root (containers, CI)
    return args


def _playwright_toolset() -> McpToolset:
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(command="npx", args=_server_args(), env=_server_env()),
            timeout=120,  # first run downloads the server
        ),
        tool_filter=PLAYWRIGHT_TOOLS,
    )


def _text(content) -> str:
    return " ".join(p.text or "" for p in (getattr(content, "parts", None) or []))


def _say(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def _truncate_result(tool_response: Any) -> tuple[Any, bool]:
    """Cap each text part of an MCP result. Returns (possibly new response, was_truncated)."""
    if not isinstance(tool_response, dict):
        return tool_response, False
    trimmed = copy.deepcopy(tool_response)
    changed = False
    for part in trimmed.get("content") or []:
        text = part.get("text") if isinstance(part, dict) else None
        if text and len(text) > TOOL_RESULT_MAX_CHARS:
            part["text"] = text[:TOOL_RESULT_MAX_CHARS] + "\n...[truncated]"
            changed = True
    if changed:
        trimmed.pop("structuredContent", None)  # would duplicate the full text
    return trimmed, changed


def _evidence_text(value: Any) -> str:
    """The plain text of a tool result (not json.dumps, whose escaped newlines would glue onto URLs)."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_evidence_text(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_evidence_text(v) for v in value)
    return str(value)


def _make_agent_callbacks(ledger: RunLedger):
    def resolve_target(callback_context):
        """Runs first on every user message: bind this run to the URL the user gave."""
        url = target_from_message(_text(callback_context.user_content))
        if not url:
            if callback_context.state.get(TARGET_KEY):
                return None  # a follow-up turn: keep the current target
            return _say(ASK_FOR_TARGET)  # ends the turn without spending a model call
        callback_context.state[TARGET_KEY] = url
        ledger.start(url, callback_context.invocation_id)
        return None

    return resolve_target


def _make_callbacks(ledger: RunLedger):
    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        # Argument caps and scope checks run before the budget is spent, so a rejected call is free.
        if tool.name == "browser_navigate":
            url = str(args.get("url", ""))[:URL_MAX_CHARS]
            args["url"] = url
            if not in_scope(url, ledger.target_url):
                ledger.blocked.append(f"navigate {url}")
                return {"error": f"Blocked: {url} is outside the target site {ledger.target_url}. Stay on the target."}
            if not ledger.record_page(url):
                ledger.blocked.append(f"page budget {url}")
                return {"error": f"Page budget of {ledger.max_pages} pages reached. Write the report now."}
        if tool.name == "browser_press_key" and args.get("key") not in ALLOWED_KEYS:
            ledger.blocked.append(f"key {args.get('key')}")
            return {"error": "Only Tab and Shift+Tab are allowed: this audit is read-only."}
        args.pop("filename", None)  # snapshots can be saved to disk; the report needs the text inline instead
        if not ledger.record_tool_call(tool.name):
            # Returning a dict skips the real call; the model sees this instead.
            return {"error": f"Tool budget of {ledger.max_tool_calls} calls reached. Write the report now."}
        return None

    def after_tool(tool, args, tool_context, tool_response) -> dict | None:
        trimmed, changed = _truncate_result(tool_response)
        # Evidence is what the model actually saw, so the post-run checks cannot be satisfied by text it never read.
        ledger.record_evidence(_evidence_text(trimmed))
        return trimmed if changed else None

    def after_model(callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage:
            ledger.record_usage(usage.prompt_token_count, usage.candidates_token_count)
        return None

    return before_tool, after_tool, after_model


def build_agent(ledger: RunLedger) -> Agent:
    before_tool, after_tool, after_model = _make_callbacks(ledger)
    return Agent(
        name="a11y_auditor",
        model=ledger.model,
        description="Audits a page's accessibility in a real browser against a fixed WCAG rubric.",
        instruction=INSTRUCTION,  # {target_url} is filled from session state by ADK
        tools=[_playwright_toolset()],
        before_agent_callback=_make_agent_callbacks(ledger),
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        after_model_callback=after_model,
    )


def default_ledger() -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "12")),
        max_pages=int(os.getenv("MAX_PAGES", "3")),
    )


# Entry point for `adk web` / `adk run`: the target comes from each chat message.
root_agent = build_agent(default_ledger())
