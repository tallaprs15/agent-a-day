"""Web QA Explorer: explores a web app in a real browser and files an evidence-backed bug report.

One agent drives Playwright MCP over stdio. Observing tools (navigate, snapshot, console,
network) run freely. Clicks and typing go through ADK's `require_confirmation`: a click on
"Delete", "Pay" or "Clear completed", or typing into a password field, pauses the run until a
human approves it. Guardrails in callbacks keep the agent on the target site and within budget.
After the run, the ledger checks that every quote in the report appeared in a tool result.

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
from .risk import ACTING_TOOLS, in_scope, is_risky, label_of

# Pinned so a run today behaves the same as a run next month.
PLAYWRIGHT_SERVER = "@playwright/mcp@0.0.83"

# Least privilege: the server exposes ~30 tools, including arbitrary JavaScript
# (browser_evaluate, browser_run_code_unsafe), file upload and downloads.
# A QA pass needs to look, click, type and read the console and network log.
PLAYWRIGHT_TOOLS = [
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_wait_for",
    "browser_navigate_back",
    "browser_console_messages",
    "browser_network_requests",
]

# The target lives in session state, set from each user message (see _make_agent_callbacks).
TARGET_KEY = "target_url"
FOCUS_KEY = "focus"
ASK_FOR_TARGET = (
    "Which web app should I test? Send a URL, optionally with what to look at, e.g. "
    "\"https://demo.playwright.dev/todomvc/ add two todos and complete one\"."
)
DEFAULT_FOCUS = "the main user flow on the page"

TYPE_TEXT_MAX_CHARS = 200
WAIT_MAX_SECONDS = 5
URL_MAX_CHARS = 500
TOOL_RESULT_MAX_CHARS = 6000  # per text part; keeps one big page snapshot from dominating the context

INSTRUCTION = """\
You are Web QA Explorer, a careful QA engineer. Test the web app below in a real browser and report
defects you can prove.

Target: {target_url}
Focus: {focus}

Process:
1. browser_navigate to the target. Read the snapshot it returns.
2. Exercise the focus with browser_click and browser_type, using the element refs from the snapshot.
   Pass a short, honest `element` description for every click or type (for example "Delete button for
   todo 'Buy milk'"). Some actions need a human's approval; if one is rejected, do not retry it or work
   around it. Note it under Coverage as "not exercised" and carry on.
3. Check browser_console_messages (level "error") and browser_network_requests (failed requests) after
   loading and after your interactions.
4. Stay on the target site. Do not leave it. Use at most 14 tool calls and never repeat an identical call.
5. Write the report.

Rules:
- A finding must be something a tool showed you: a console error, a failed request, a missing label,
  a broken interaction. Do not guess or invent. "No defects found" is a valid result.
- Every finding needs an Evidence line with at least one short quote, in backticks, copied exactly from
  a tool result.
- Only list URLs you actually visited or that a tool returned.

Output format (Markdown), with these exact headings:
## Summary      (2-3 sentences: what you tested and what you found)
## Findings
### BUG-1 · <short title>
- **Severity:** high | medium | low
- **Steps:** <what you did, in order>
- **Expected / actual:** <one line each>
- **Evidence:** `exact quote from a tool result`
(repeat for each finding; write "No defects found." if there are none)
## Coverage     (pages visited, actions exercised, actions not exercised and why)
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
    args = ["-y", PLAYWRIGHT_SERVER, "--headless", "--isolated", "--output-dir", tempfile.mkdtemp(prefix="web-qa-")]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")  # Chromium refuses to start its sandbox as root (containers, CI)
    return args


def needs_approval(**args: Any) -> bool:
    """`require_confirmation` hook. ADK passes the tool call's arguments as keywords.

    Only the acting tools (click, type) carry an `element` label; the observing tools have none.
    """
    return bool(args.get("element") or args.get("target") or args.get("ref")) and is_risky(args)


def _playwright_toolset() -> McpToolset:
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(command="npx", args=_server_args(), env=_server_env()),
            timeout=120,  # first run downloads the server
        ),
        tool_filter=PLAYWRIGHT_TOOLS,
        require_confirmation=needs_approval,
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


def _describe(tool_name: str, args: dict[str, Any]) -> str:
    return f"{tool_name}({label_of(args)})" + (f" text={args['text']!r}" if "text" in args else "")


def _make_agent_callbacks(ledger: RunLedger):
    def resolve_target(callback_context):
        """Runs first on every user message: bind this run to the URL (and focus) the user gave."""
        parsed = target_from_message(_text(callback_context.user_content))
        if not parsed:
            if callback_context.state.get(TARGET_KEY):
                return None  # a follow-up turn, such as a confirmation reply: keep the current target
            return _say(ASK_FOR_TARGET)  # ends the turn without spending a model call
        url, focus = parsed
        if ledger.invocation_id == callback_context.invocation_id:
            # Resuming after an approval: ADK re-runs this callback with the original message.
            # Resetting here would wipe the budget, evidence and approvals gathered so far.
            return None
        callback_context.state[TARGET_KEY] = url
        callback_context.state[FOCUS_KEY] = focus or DEFAULT_FOCUS
        ledger.start(url, focus or DEFAULT_FOCUS, callback_context.invocation_id)
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
        args.pop("filename", None)  # observing tools can save to disk; the report needs the text inline instead
        if tool.name == "browser_type" and isinstance(args.get("text"), str):
            args["text"] = args["text"][:TYPE_TEXT_MAX_CHARS]
        if tool.name == "browser_wait_for" and args.get("time") is not None:
            args["time"] = min(float(args["time"]), WAIT_MAX_SECONDS)

        if tool.name in ACTING_TOOLS and needs_approval(**args):
            confirmation = getattr(tool_context, "tool_confirmation", None)
            if confirmation is None:
                return None  # first attempt: the tool itself pauses the run to ask a human; costs no budget
            ledger.record_approval(_describe(tool.name, args), bool(confirmation.confirmed))
            if not confirmation.confirmed:
                return None  # the tool returns "rejected"; nothing runs

        if not ledger.record_tool_call(tool.name):
            # Returning a dict skips the real call; the model sees this instead.
            return {"error": f"Tool budget of {ledger.max_tool_calls} calls reached. Write the report now."}
        return None

    def after_tool(tool, args, tool_context, tool_response) -> dict | None:
        trimmed, changed = _truncate_result(tool_response)
        # Evidence is what the model actually saw, so the post-run checks cannot be satisfied by text it never read.
        ledger.record_evidence(_evidence_text(trimmed))
        confirmation = getattr(tool_context, "tool_confirmation", None)
        ran = not (isinstance(tool_response, dict) and "error" in tool_response and "content" not in tool_response)
        if ran and tool.name in ACTING_TOOLS and needs_approval(**args) and not (confirmation and confirmation.confirmed):
            ledger.unapproved_risky.append(_describe(tool.name, args))  # should be impossible; the check proves it
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
        name="web_qa_explorer",
        model=ledger.model,
        description="Explores a web app in a real browser and files an evidence-backed bug report.",
        instruction=INSTRUCTION,  # {target_url} and {focus} are filled from session state by ADK
        tools=[_playwright_toolset()],
        before_agent_callback=_make_agent_callbacks(ledger),
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        after_model_callback=after_model,
    )


def default_ledger() -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "14")),
        max_pages=int(os.getenv("MAX_PAGES", "5")),
    )


# Entry point for `adk web` / `adk run`: the target comes from each chat message.
root_agent = build_agent(default_ledger())
