"""arXiv Scout: researches a topic on arXiv and writes a cited engineering brief.

Tools come from two public MCP servers launched over stdio with uvx:
  * arxiv-mcp-server: search arXiv, read abstracts, export BibTeX
  * mcp-server-fetch (official reference server): read project pages / READMEs
"""

from __future__ import annotations

import json
import os
from typing import Any

from google.adk.agents import Agent
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from mcp import StdioServerParameters

from .ledger import RunLedger

# Pinned so a run today behaves the same as a run next month.
ARXIV_SERVER = "arxiv-mcp-server==0.8.2"
FETCH_SERVER = "mcp-server-fetch==2026.10.10"

# Least privilege: the arXiv server exposes ~20 tools (downloads, watches,
# local indexes). The scout only needs read-only discovery.
ARXIV_TOOLS = ["search_papers", "get_abstract", "export_citations"]
FETCH_TOOLS = ["fetch"]

INSTRUCTION = """\
You are arXiv Scout, a research assistant for software engineers.

Given a topic, produce a concise engineering brief based ONLY on what your tools return.

Process:
1. Call search_papers with 1-2 focused queries (max_results <= 8). Prefer recent work.
2. Pick the 3-5 most relevant papers. Call get_abstract for each one you will cite.
3. Optionally fetch at most 2 project pages or GitHub READMEs linked from abstracts.
4. Write the brief.

Rules:
- Every paper you mention must be one returned by a tool in this session.
  Cite it as [arXiv:ID](https://arxiv.org/abs/ID). Never invent IDs, titles or numbers.
- If the tools return little, say so. A short honest brief beats a padded one.
- You have a strict tool-call budget. Do not repeat identical calls.

Output format (Markdown):
# <Topic>: engineering brief
## TL;DR            (3 bullets)
## Key papers       (table: Paper | Core idea | Why it matters for builders)
## Techniques worth trying
## Open problems / caveats
## Sources          (bulleted citation links)
"""


def _arxiv_toolset() -> McpToolset:
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(command="uvx", args=[ARXIV_SERVER]),
            timeout=60,  # first run downloads the server
        ),
        tool_filter=ARXIV_TOOLS,
    )


def _fetch_toolset() -> McpToolset:
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(command="uvx", args=[FETCH_SERVER]),
            timeout=60,
        ),
        tool_filter=FETCH_TOOLS,
    )


def _make_callbacks(ledger: RunLedger):
    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        if not ledger.record_tool_call(tool.name):
            # Returning a dict skips the real call; the model sees this instead.
            return {"error": f"Tool budget of {ledger.max_tool_calls} calls reached. Write the brief now."}
        if tool.name == "fetch":
            args["max_length"] = min(int(args.get("max_length") or 8000), 8000)
        return None

    def after_tool(tool, args, tool_context, tool_response) -> dict | None:
        ledger.record_evidence(json.dumps(tool_response, default=str))
        return None

    def after_model(callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage:
            ledger.record_usage(usage.prompt_token_count, usage.candidates_token_count)
        return None

    return before_tool, after_tool, after_model


def build_agent(ledger: RunLedger) -> Agent:
    before_tool, after_tool, after_model = _make_callbacks(ledger)
    return Agent(
        name="arxiv_scout",
        model=ledger.model,
        description="Researches a topic on arXiv and writes a cited engineering brief.",
        instruction=INSTRUCTION,
        tools=[_arxiv_toolset(), _fetch_toolset()],
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        after_model_callback=after_model,
    )


def default_ledger() -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "12")),
    )


# Entry point for `adk web` / `adk run`.
root_agent = build_agent(default_ledger())
