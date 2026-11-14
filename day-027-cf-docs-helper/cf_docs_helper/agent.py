"""CF Docs Helper: answers Cloudflare Workers questions with links to the official docs.

The only tool source is Cloudflare's own docs MCP server, a *remote* server reached over
streamable HTTP (no subprocess, no install, no auth):

  https://docs.mcp.cloudflare.com/mcp  ->  search_cloudflare_documentation
"""

from __future__ import annotations

import json
import os
from typing import Any

from google.adk.agents import Agent
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from google.genai import types

from .ledger import RunLedger

# Remote and unversioned, so there is nothing to pin: the smoke test guards the tool list instead.
DOCS_URL = "https://docs.mcp.cloudflare.com/mcp"

# Least privilege: the server also offers a Pages-to-Workers migration guide, which is out of scope.
DOCS_TOOLS = ["search_cloudflare_documentation"]
MAX_QUERY_CHARS = 200

ASK_FOR_QUESTION = "What would you like to know about Cloudflare Workers or the Cloudflare developer platform?"

INSTRUCTION = """\
You are CF Docs Helper, an assistant for developers building on Cloudflare (Workers, KV, R2, D1,
Durable Objects, Queues, Pages and the rest of the developer platform).

Answer the user's question using ONLY what search_cloudflare_documentation returns.

Process:
1. Call search_cloudflare_documentation with 1-3 focused queries (short keyword phrases, not full sentences).
2. If results are thin, try one differently-worded query. Do not repeat identical queries.
3. Write the answer.

Rules:
- Quote limits, prices, config keys and commands exactly as the docs show them. Never guess a number.
- Link only to pages the tool returned, as https://developers.cloudflare.com/<path>/ (the result's <url>,
  or a /path/ link that appears inside a result). Never invent a URL.
- If the docs do not cover the question, say so plainly. A short honest answer beats a padded one.
- Questions unrelated to Cloudflare: say this assistant only covers Cloudflare, and do not call tools.
- You have a strict tool-call budget.

Output format (Markdown):
## Answer       (direct answer first, then details; code blocks for config and commands)
## Sources      (bulleted links to the doc pages you used, each with a short title)
"""


def _docs_toolset() -> McpToolset:
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=DOCS_URL, timeout=60, sse_read_timeout=120),
        tool_filter=DOCS_TOOLS,
    )


def _text(content) -> str:
    return " ".join(p.text or "" for p in (getattr(content, "parts", None) or []))


def _say(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def _make_callbacks(ledger: RunLedger):
    def check_question(callback_context):
        """Runs first on every user message: no question, no model call."""
        if not _text(callback_context.user_content).strip():
            return _say(ASK_FOR_QUESTION)
        return None

    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        # Argument guards run before the budget is spent, so a rejected call is free.
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "query is required."}
        args["query"] = query[:MAX_QUERY_CHARS]
        if not ledger.record_tool_call(tool.name):
            # Returning a dict skips the real call; the model sees this instead.
            return {"error": f"Tool budget of {ledger.max_tool_calls} calls reached. Answer with what you have."}
        return None

    def after_tool(tool, args, tool_context, tool_response) -> dict | None:
        ledger.record_evidence(json.dumps(tool_response, default=str))
        return None

    def after_model(callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage:
            ledger.record_usage(usage.prompt_token_count, usage.candidates_token_count)
        return None

    return check_question, before_tool, after_tool, after_model


def build_agent(ledger: RunLedger) -> Agent:
    check_question, before_tool, after_tool, after_model = _make_callbacks(ledger)
    return Agent(
        name="cf_docs_helper",
        model=ledger.model,
        description="Answers Cloudflare developer-platform questions with links to the official docs.",
        instruction=INSTRUCTION,
        tools=[_docs_toolset()],
        before_agent_callback=check_question,
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        after_model_callback=after_model,
    )


def default_ledger() -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "6")),
    )


# Entry point for `adk web` / `adk run`: the question comes from each chat message.
root_agent = build_agent(default_ledger())
