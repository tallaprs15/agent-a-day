"""A2A Duo: two ADK agents that collaborate over the Agent2Agent (A2A) protocol.

  librarian  owns the arXiv MCP tools; exposed as an A2A server (agent card + JSON-RPC over HTTP)
  writer     owns no tools except the librarian: it calls it through RemoteA2aAgent (wrapped in
             AgentTool) and writes the cited brief

The writer never sees the arXiv server and the librarian never sees the user's request format;
the only contract between them is the A2A agent card and the text they exchange. Tools come from
the public arxiv-mcp-server (stdio via uvx), on the librarian side only.
"""

from __future__ import annotations

import os
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.remote_a2a_agent import AGENT_CARD_WELL_KNOWN_PATH, RemoteA2aAgent
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from mcp import StdioServerParameters

from .ledger import RunLedger

# Pinned so a run today behaves the same as a run next month.
ARXIV_SERVER = "arxiv-mcp-server==0.8.2"

# Least privilege: the arXiv server exposes ~20 tools (downloads, watches, local indexes).
# The librarian only needs read-only discovery.
ARXIV_TOOLS = ["search_papers", "get_abstract"]

LIBRARIAN_INSTRUCTION = """\
You are the Librarian, a research agent reachable over A2A. Another agent sends you a topic.
Find the most relevant recent arXiv papers using ONLY your tools.

1. Call search_papers once or twice (max_results <= 8). Prefer recent work.
2. Call get_abstract for the 3-4 best papers.
3. Reply with a plain list, one line per paper, exactly:
   - arXiv:<id> | <title> | <what it shows, one sentence, from the abstract>
Never invent IDs or titles. If the tools return little, say so. No other prose.
"""

WRITER_INSTRUCTION = """\
You are the Writer, an engineering-brief author. The user gives you a research topic in their message.
If they have not given one, ask which topic they want and stop.

You have no search tools. Call the `librarian` tool at most twice, passing the topic as the request
(refine the request only if the first reply is thin). It is a separate agent reached over A2A.
Then write the brief using ONLY what the librarian returned.

Rules:
- Cite papers as [arXiv:ID](https://arxiv.org/abs/ID), only IDs the librarian returned.
  Never invent IDs, titles or numbers.
- If the librarian returns little, say so. A short honest brief beats a padded one.

Output format (Markdown):
# <Topic>: engineering brief
## TL;DR            (3 bullets)
## Key papers       (table: Paper | Core idea | Why it matters for builders)
## Techniques worth trying
## Caveats
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


def _librarian_callbacks(ledger: RunLedger):
    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        if not ledger.record_tool_call(tool.name):
            return {"error": f"Tool budget of {ledger.max_tool_calls} calls reached. Reply with what you have."}
        if tool.name == "search_papers":
            args["max_results"] = min(int(args.get("max_results") or 8), 8)
        return None

    def after_tool(tool, args, tool_context, tool_response) -> dict | None:
        ledger.record_mcp_evidence(str(tool_response))
        return None

    return before_tool, after_tool


def _usage_callback(ledger: RunLedger):
    def after_model(callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage:
            ledger.record_usage(usage.prompt_token_count, usage.candidates_token_count)
        return None

    return after_model


def build_librarian(ledger: RunLedger) -> Agent:
    before_tool, after_tool = _librarian_callbacks(ledger)
    return Agent(
        name="librarian",
        model=ledger.model,
        description="Finds recent arXiv papers on a topic and returns id | title | finding lines.",
        instruction=LIBRARIAN_INSTRUCTION,
        tools=[_arxiv_toolset()],
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        after_model_callback=_usage_callback(ledger),
    )


def card_url(port: int, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{port}{AGENT_CARD_WELL_KNOWN_PATH}"


def build_writer(ledger: RunLedger, librarian_card_url: str) -> Agent:
    remote = RemoteA2aAgent(
        name="librarian",
        description="Remote research agent (A2A). Send it a topic; it returns arXiv papers as id | title | finding.",
        agent_card=librarian_card_url,
    )

    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        if not ledger.record_delegation():
            return {"error": f"Delegation cap of {ledger.max_delegations} reached. Write the brief now."}
        if "request" in args:
            args["request"] = str(args["request"])[:300]  # argument cap: topics, not essays
        return None

    def after_tool(tool, args, tool_context, tool_response) -> dict | None:
        ledger.record_librarian_reply(str(tool_response))
        return None

    return Agent(
        name="writer",
        model=ledger.model,
        description="Writes a cited engineering brief using a remote librarian agent.",
        instruction=WRITER_INSTRUCTION,
        tools=[AgentTool(agent=remote)],
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        after_model_callback=_usage_callback(ledger),
    )


def default_ledger() -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "8")),
    )


# Entry point for `adk web` / `adk run`: the writer, talking to a librarian already serving at
# LIBRARIAN_URL (start it with `python -m a2a_duo serve`). The topic comes from the chat message.
root_agent = build_writer(default_ledger(), os.getenv("LIBRARIAN_URL", card_url(8001)))
