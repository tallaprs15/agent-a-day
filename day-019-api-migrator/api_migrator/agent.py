"""API Migrator: a migration plan between two versions of a library, reasoned from a diff of their docs.

SequentialAgent(ParallelAgent(old_researcher, new_researcher), differ), joined by session state:

  old_researcher  docs for the FROM version  -> state["old_docs"]  \
                                                                     } run concurrently
  new_researcher  docs for the TO version    -> state["new_docs"]  /
  differ          (no tools) compares the two -> state["plan"]

The differ never touches a tool: it can only reason over the two digests. Afterwards the ledger
checks the plan against per-side evidence: each removed API must appear in the old docs, each
replacement in the new docs. Docs come from two public MCP servers:
  * Context7 (stdio via npx): current and per-version docs for open-source libraries
  * Microsoft Learn (remote, streamable HTTP, no auth): Microsoft and Azure docs
"""

from __future__ import annotations

import copy
import os
from typing import Any

from google.adk.agents import Agent, ParallelAgent, SequentialAgent
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams, StreamableHTTPConnectionParams
from google.genai import types
from mcp import StdioServerParameters

from .ledger import RunLedger, request_from_message, side_of

# Pinned so a run today behaves the same as a run next month.
CONTEXT7_SERVER = "@upstash/context7-mcp@4.4.4"
LEARN_URL = "https://learn.microsoft.com/api/mcp"  # remote, unversioned: the smoke test guards its tool list

# Least privilege: Context7 needs both tools (resolve a library, then query it).
# From Microsoft Learn we take search and code samples, not the page fetcher (large, unbounded pages).
CONTEXT7_TOOLS = ["resolve-library-id", "query-docs"]
LEARN_TOOLS = ["microsoft_docs_search", "microsoft_code_sample_search"]

REQUEST_KEY = "request"
ASK_FOR_REQUEST = (
    "Which library, and which versions? Name the library and both versions, e.g. "
    "\"Migrate Pydantic from v1 to v2\" or \"Upgrade the Azure Storage Blob Python SDK from 2.1 to 12\"."
)

QUERY_MAX_CHARS = 300
TOOL_RESULT_MAX_CHARS = 6000  # per text part; two researchers feed the differ, so each digest stays small

RESEARCHER_COMMON = """\
The user's migration request:
{request}

Use at most 3 tool calls in total and never repeat an identical call.
- Open-source libraries and frameworks: call resolve-library-id (libraryName = the library, query = the
  docs you need), then query-docs with a libraryId pinned to the %(side_version)s version when the
  results list one (ids look like /owner/project/version). If Context7 returns an error (for example its
  free quota is used up), copy the error text into the digest and carry on with Microsoft Learn if the
  library is a Microsoft or Azure one.
- Microsoft or Azure products (.NET, Azure SDKs, Windows...): call microsoft_docs_search and
  microsoft_code_sample_search with the version in the query text.

Output a digest in Markdown. State which version the docs you found actually describe (they may not match
the one requested: say so). For every source include its exact id or URL as the tools returned it, and
copy the relevant API names, signatures and snippets verbatim. Add nothing the tools did not return.
If the tools found nothing relevant for this version, output "NO RELEVANT DOCS FOUND".
"""

OLD_INSTRUCTION = (
    "You are the OLD-version researcher of an API migration pipeline. Gather evidence about the library\n"
    "version the user is migrating FROM. Do not write the migration plan.\n\n"
) + RESEARCHER_COMMON % {"side_version": "FROM"}

NEW_INSTRUCTION = (
    "You are the NEW-version researcher of an API migration pipeline. Gather evidence about the library\n"
    "version the user is migrating TO. Include what the docs say about migrating or breaking changes.\n"
    "Do not write the migration plan.\n\n"
) + RESEARCHER_COMMON % {"side_version": "TO"}

DIFFER_INSTRUCTION = """\
You are the differ stage of an API migration pipeline. You have no tools.

The user's migration request:
{request}

Documentation for the FROM version:
{old_docs}

Documentation for the TO version:
{new_docs}

Write a migration plan by comparing the two documentation sets above. Use ONLY them.

Rules:
- Put an API in the "Old API" column only if it appears in the FROM documentation. Put an API in the
  "New API" column only if it appears in the TO documentation. Write every API name in backticks.
- If an old API has no replacement in the TO docs, write "removed" in the New API column (no backticks).
- If the two documentation sets do not show a difference that matters, or either says
  "NO RELEVANT DOCS FOUND" or reports a tool error, write "The retrieved docs do not cover this" and
  stop. Do not fall back on memory.
- Keep code short. Every function or method in your code must appear in the documentation above.
- In Sources, list only ids or URLs exactly as they appear above. Copy them character for character: never
  shorten, complete or guess a URL, and omit any source you did not actually use.

Output format (Markdown), with these exact headings:
## Summary             (2-3 sentences: library, versions the docs describe, size of the migration)
## Breaking changes    (table with columns: Old API | New API | Evidence, one row per change, Evidence = a short quote or URL)
## Migration steps     (numbered, in the order to do them)
## Before and after    (one short before/after code block, only if the docs support it)
## Not covered         (anything the user would need to check elsewhere; "nothing" if so)
## Sources             (bulleted list of Context7 library ids and/or URLs you used)
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


def _evidence_text(value: Any) -> str:
    """The plain text of a tool result. Not json.dumps: its escaped "\\n" would glue onto URLs and break matching."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_evidence_text(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_evidence_text(v) for v in value)
    return str(value)


def _make_pipeline_callbacks(ledger: RunLedger):
    def resolve_request(callback_context):
        """Runs first on every user message: bind this run to the migration the user asked for."""
        request = request_from_message(_text(callback_context.user_content))
        if not request:
            return _say(ASK_FOR_REQUEST)  # ends the turn without spending a model call
        callback_context.state[REQUEST_KEY] = request
        ledger.start(request)
        return None

    return resolve_request


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
        # Evidence is what the model actually saw, filed under the side (old/new) of the agent that saw it.
        side = side_of(tool_context.agent_name)
        if side:
            ledger.record_evidence(side, _evidence_text(trimmed))
        return trimmed if changed else None

    def after_model(callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage:
            ledger.record_usage(usage.prompt_token_count, usage.candidates_token_count)
        return None

    return before_tool, after_tool, after_model


def build_agent(ledger: RunLedger) -> SequentialAgent:
    before_tool, after_tool, after_model = _make_callbacks(ledger)
    resolve_request = _make_pipeline_callbacks(ledger)
    common = dict(model=ledger.model, after_model_callback=after_model)

    def researcher(side: str, instruction: str, key: str) -> Agent:
        # Each researcher owns its own MCP connections, so the two can run concurrently.
        return Agent(
            name=f"{side}_researcher",
            description=f"Retrieves the {side}-version documentation for the migration.",
            instruction=instruction,
            tools=[_context7_toolset(), _learn_toolset()],
            before_tool_callback=before_tool,
            after_tool_callback=after_tool,
            output_key=key,
            **common,
        )

    gather = ParallelAgent(
        name="gather_docs",
        description="Retrieves both versions' docs concurrently.",
        sub_agents=[
            researcher("old", OLD_INSTRUCTION, "old_docs"),
            researcher("new", NEW_INSTRUCTION, "new_docs"),
        ],
    )
    differ = Agent(
        name="differ",
        description="Diffs the two doc sets into a migration plan.",
        instruction=DIFFER_INSTRUCTION,
        output_key="plan",
        **common,
    )
    return SequentialAgent(
        name="api_migrator",
        description="Migration plan between two versions of a library, reasoned from a diff of their docs.",
        sub_agents=[gather, differ],
        before_agent_callback=resolve_request,
    )


def default_ledger(request: str | None = None) -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        request=request or "",
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "8")),
    )


# Entry point for `adk web` / `adk run`: the request comes from each chat message.
root_agent = build_agent(default_ledger())
