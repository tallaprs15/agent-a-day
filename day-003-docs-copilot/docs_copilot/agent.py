"""Docs Copilot: version-correct answers about library APIs, grounded in retrieved docs.

A SequentialAgent runs two stages that talk through session state:

  researcher  Context7 + Microsoft Learn tools   -> state["snippets"]
  answerer    (no tools)                         -> state["answer"]

The answerer can only use what the researcher retrieved. After the run the ledger
verifies the answer: every cited source and every API call in its code must appear
in a tool result. Two public MCP servers supply the docs:
  * Context7 (stdio via npx): current docs and code snippets for open-source libraries
  * Microsoft Learn (remote, streamable HTTP, no auth): Microsoft and Azure docs
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any

from google.adk.agents import Agent, SequentialAgent
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams, StreamableHTTPConnectionParams
from google.genai import types
from mcp import StdioServerParameters

from .ledger import RunLedger, question_from_message

# Pinned so a run today behaves the same as a run next month.
CONTEXT7_SERVER = "@upstash/context7-mcp@4.4.4"
LEARN_URL = "https://learn.microsoft.com/api/mcp"  # remote, unversioned: the smoke test guards its tool list

# Least privilege: Context7 needs both of its tools (resolve a library, then query it).
# From Microsoft Learn we take search and code samples, not the page fetcher (large, unbounded pages).
CONTEXT7_TOOLS = ["resolve-library-id", "query-docs"]
LEARN_TOOLS = ["microsoft_docs_search", "microsoft_code_sample_search"]

# The question lives in session state, set from each user message (see _make_pipeline_callbacks).
QUESTION_KEY = "question"
ASK_FOR_QUESTION = (
    "What would you like to know? Ask about a library or API, e.g. "
    "\"How do I declare a dependency in FastAPI?\" or \"How do I upload a blob with the Azure Storage Python SDK?\""
)

QUERY_MAX_CHARS = 300  # search/query strings: long queries only dilute the retrieval
TOOL_RESULT_MAX_CHARS = 7000  # per text part; keeps one big doc dump from dominating the context

RESEARCHER_INSTRUCTION = """\
You are the researcher stage of a documentation question-answering pipeline.

The user's question:
{question}

Gather evidence with your tools. Do not answer the question yourself.
1. Open-source libraries and frameworks: call resolve-library-id (libraryName = the library, query = the
   question), pick the best match (prefer an exact name and a high benchmark score), then call query-docs
   with that libraryId and a focused query. If the user named a version, prefer a library id for that version.
   If Context7 returns an error (for example its free quota is used up), copy the error text into the digest;
   for a Microsoft or Azure question, carry on with Microsoft Learn instead.
2. Microsoft or Azure products (.NET, Azure SDKs, Windows, Microsoft 365...): call microsoft_docs_search
   and, for code, microsoft_code_sample_search.
3. Use at most 4 tool calls in total. Never repeat an identical call.

Output a digest in Markdown. For every source, include its exact id or URL as the tools returned it, the
library version if the docs state one, and the relevant snippets copied verbatim. Add nothing the tools
did not return. If the tools found nothing relevant, output "NO RELEVANT DOCS FOUND".
"""

ANSWERER_INSTRUCTION = """\
You are the answerer stage of a documentation question-answering pipeline. You have no tools.

The user's question:
{question}

Retrieved documentation:
{snippets}

Answer the question using ONLY the retrieved documentation above.

Rules:
- Use only functions, classes, parameters and URLs that appear in the documentation above. If the answer is
  not covered, say "The retrieved docs do not cover this" and stop. Do not fall back on memory.
  If the documentation reports a tool error, say what it was.
- State the library version the docs refer to, if the documentation says so.
- Keep code short and runnable. Every API call in your code must come from the documentation.
- In Sources, list only ids or URLs exactly as they appear above.

Output format (Markdown), with these exact headings:
## Answer         (2-5 sentences, then a code block if the question calls for code)
## Version notes  (version the docs describe, deprecations or caveats; "none stated" if so)
## Sources        (bulleted list of the Context7 library ids and/or URLs you used)
"""


def _server_env() -> dict[str, str]:
    """Environment for the npx subprocess: PATH/HOME, the optional free Context7 key, and proxy/CA settings."""
    from mcp.client.stdio import get_default_environment

    env = get_default_environment()
    for key in ("CONTEXT7_API_KEY", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                "https_proxy", "http_proxy", "no_proxy"):
        if os.getenv(key):
            env[key] = os.environ[key]
    return env


def _context7_toolset() -> McpToolset:
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(command="npx", args=["-y", CONTEXT7_SERVER], env=_server_env()),
            timeout=90,  # first run downloads the server
        ),
        tool_filter=CONTEXT7_TOOLS,
    )


def _learn_toolset() -> McpToolset:
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=LEARN_URL, timeout=60, sse_read_timeout=120),
        tool_filter=LEARN_TOOLS,
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


def _make_pipeline_callbacks(ledger: RunLedger):
    def resolve_question(callback_context):
        """Runs first on every user message: bind this run to the question the user asked."""
        question = question_from_message(_text(callback_context.user_content))
        if not question:
            return _say(ASK_FOR_QUESTION)  # ends the turn without spending a model call
        callback_context.state[QUESTION_KEY] = question
        ledger.question = question
        return None

    return resolve_question


def _make_callbacks(ledger: RunLedger):
    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        # Argument caps run before the budget is spent, so a rejected call is free.
        for key in ("query", "libraryName"):
            if isinstance(args.get(key), str):
                args[key] = args[key][:QUERY_MAX_CHARS]
        if tool.name == "query-docs" and not str(args.get("libraryId", "")).startswith("/"):
            return {"error": "libraryId must be a Context7 id such as /owner/project. Call resolve-library-id first."}
        if not ledger.record_tool_call(tool.name):
            # Returning a dict skips the real call; the model sees this instead.
            return {"error": f"Tool budget of {ledger.max_tool_calls} calls reached. Finish with what you have."}
        return None

    def after_tool(tool, args, tool_context, tool_response) -> dict | None:
        trimmed, changed = _truncate_result(tool_response)
        # Evidence is what the model actually saw, so the post-run checks cannot be satisfied by text it never read.
        ledger.record_evidence(json.dumps(trimmed, default=str))
        return trimmed if changed else None

    def after_model(callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage:
            ledger.record_usage(usage.prompt_token_count, usage.candidates_token_count)
        return None

    return before_tool, after_tool, after_model


def build_agent(ledger: RunLedger) -> SequentialAgent:
    before_tool, after_tool, after_model = _make_callbacks(ledger)
    resolve_question = _make_pipeline_callbacks(ledger)
    common = dict(model=ledger.model, after_model_callback=after_model)
    # {question}, {snippets} are filled from session state by ADK at run time.
    researcher = Agent(
        name="researcher",
        description="Retrieves current documentation snippets for the question.",
        instruction=RESEARCHER_INSTRUCTION,
        tools=[_context7_toolset(), _learn_toolset()],
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        output_key="snippets",
        **common,
    )
    answerer = Agent(
        name="answerer",
        description="Answers from the retrieved docs only, with sources.",
        instruction=ANSWERER_INSTRUCTION,
        output_key="answer",
        **common,
    )
    return SequentialAgent(
        name="docs_copilot",
        description="Version-correct answers to library API questions, grounded in retrieved docs.",
        sub_agents=[researcher, answerer],
        before_agent_callback=resolve_question,
    )


def default_ledger(question: str | None = None) -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        question=question or "",
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "6")),
    )


# Entry point for `adk web` / `adk run`: the question comes from each chat message.
root_agent = build_agent(default_ledger())
