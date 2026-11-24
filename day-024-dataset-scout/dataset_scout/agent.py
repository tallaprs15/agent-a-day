"""Dataset Scout: finds Hugging Face datasets for a task and explains them in a grounded guide.

Pattern: retrieval + summarisation. The agent first retrieves (search, then structure and a few sample rows),
then summarises. Retrieval and summarisation are separated by evidence: the guide may only cite datasets that
a tool returned, and the CLI fails the run when it does not.

  resolve_request (callback)  user message -> state["request"]; asks a question when it is too short
  dataset_scout (Agent + HF)  search -> inspect structure -> peek at rows -> write the guide
  guardrails (callbacks)      dataset-only searches, argument caps, tool budget, tool output size cap, meter
"""

from __future__ import annotations

import json
import os
from typing import Any

from google.adk.agents import Agent
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from google.genai import types

from .ledger import MAX_REQUEST_CHARS, MIN_REQUEST_CHARS, RunLedger, truncate_response

# The public Hugging Face MCP server is hosted by Hugging Face; its version is reported at initialize (0.4.34 when verified).
HF_MCP_URL = "https://huggingface.co/mcp"

# Least privilege: the server also exposes account inspection and a filesystem-style tool (hf_fs).
# Finding and explaining datasets needs search and read-only details, nothing else.
HF_TOOLS = ["hub_repo_search", "hub_repo_details"]

REQUEST_KEY = "request"
ASK_FOR_REQUEST = (
    "What do you need a dataset for? Describe the task and any constraints, for example: "
    '"fine-tune a small model to classify customer support tickets, English, permissive licence".'
)

SEARCH_MAX_LIMIT = 8
DETAILS_MAX_REPOS = 3
PREVIEW_MAX_ROWS = 3
QUERY_MAX = 200
TOOL_OUTPUT_MAX_CHARS = 9000
ALLOWED_OPERATIONS = {"overview", "dataset_structure", "dataset_preview"}

INSTRUCTION = """\
You are Dataset Scout, an assistant that finds and explains Hugging Face datasets. The user's request:

{request}

Use ONLY facts your tools return. Tools: `hub_repo_search` (arguments: query, filters, sort, limit; it searches
datasets only) and `hub_repo_details` (arguments: repo_ids, operations, config, split).

Process (strict tool budget; never repeat an identical call):
1. Search with 1-2 short keyword queries (use `filters` such as `language:en` or `task_categories:text-classification`
   only when the request implies them). Sorting by downloads surfaces established datasets.
2. Pick 3-4 promising datasets. Call `hub_repo_details` with operations ["overview", "dataset_structure"] for them
   to confirm licence, size, splits and columns.
3. Optionally call `hub_repo_details` with operations ["dataset_preview"] (config and split from the structure,
   limit 3) for the single best candidate, to describe what a row looks like.
4. Write the guide.

Rules:
- Every dataset you mention must have been returned by a tool in this session. Never invent ids, sizes, splits,
  columns or licences. If a fact is missing, write "not stated".
- Link each dataset as [author/name](https://huggingface.co/datasets/author/name).
- State licence plainly. If it is non-permissive or missing, say what to check before commercial use.
- Prefer an honest short guide over a padded one. If nothing fits, say so and suggest a better query.

Output format (Markdown):
# Dataset guide: <task in a few words>
> <the request, one line>
## Recommendation          (1-2 sentences: which one to start with, and why)
## Shortlist               (table: Dataset | Fit | Size | Licence | Downloads | Last updated)
## Best pick in detail     (splits and columns, one example row described in words, caveats)
## Load it                 (a `datasets` snippet: load_dataset("author/name", ...) for the best pick only)
## Before you rely on it   (2-4 bullets: licence, label quality, bias, leakage, staleness)
"""


def _text(content) -> str:
    return " ".join(p.text or "" for p in (getattr(content, "parts", None) or []))


def _say(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def _response_text(response) -> str:
    """The text an MCP tool returned. Parsing needs the Markdown at line starts, so don't JSON-encode it."""
    if isinstance(response, dict):
        parts = [c.get("text", "") for c in response.get("content") or [] if isinstance(c, dict)]
        if parts:
            return "\n".join(parts)
    return response if isinstance(response, str) else json.dumps(response, default=str, ensure_ascii=False)


def _hf_toolset() -> McpToolset:
    headers = {"Authorization": f"Bearer {os.environ['HF_TOKEN']}"} if os.getenv("HF_TOKEN") else None
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=HF_MCP_URL, headers=headers, timeout=60, sse_read_timeout=120),
        tool_filter=HF_TOOLS,
    )


def _make_resolve_request():
    def resolve_request(callback_context):
        """Runs first on every user message: bind this run to the request the user just sent."""
        text = _text(callback_context.user_content).strip()
        if len(text) < MIN_REQUEST_CHARS:
            return _say(ASK_FOR_REQUEST)  # ends the turn without spending a model call
        callback_context.state[REQUEST_KEY] = text[:MAX_REQUEST_CHARS]
        return None

    return resolve_request


def _make_callbacks(ledger: RunLedger):
    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        # Argument guards run before the budget is spent, so a rejected call is free.
        if tool.name == "hub_repo_search":
            args["query"] = str(args.get("query", ""))[:QUERY_MAX]
            args["repo_types"] = ["dataset"]  # this agent recommends datasets, not models or Spaces
            args["limit"] = min(int(args.get("limit") or SEARCH_MAX_LIMIT), SEARCH_MAX_LIMIT)
        elif tool.name == "hub_repo_details":
            args["repo_ids"] = [str(r) for r in (args.get("repo_ids") or [])][:DETAILS_MAX_REPOS]
            args["repo_type"] = "dataset"
            ops = [o for o in (args.get("operations") or ["overview"]) if o in ALLOWED_OPERATIONS]
            args["operations"] = ops or ["overview"]
            if "dataset_preview" in args["operations"]:
                args["limit"] = min(int(args.get("limit") or PREVIEW_MAX_ROWS), PREVIEW_MAX_ROWS)
        if not ledger.record_tool_call(tool.name, args):
            # Returning a dict skips the real call; the model sees this instead.
            return {"error": f"Tool budget of {ledger.max_tool_calls} calls reached. Write the guide now."}
        return None

    def after_tool(tool, args, tool_context, tool_response) -> dict | None:
        ledger.record_evidence(_response_text(tool_response))
        # Sample rows and long cards can be huge; cap what the model has to read (and pay for).
        capped = truncate_response(tool_response, TOOL_OUTPUT_MAX_CHARS)
        return capped if capped is not tool_response and capped != tool_response else None

    def after_model(callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage:
            ledger.record_usage(usage.prompt_token_count, usage.candidates_token_count)
        return None

    return before_tool, after_tool, after_model


def build_agent(ledger: RunLedger) -> Agent:
    before_tool, after_tool, after_model = _make_callbacks(ledger)
    return Agent(
        name="dataset_scout",
        description="Finds Hugging Face datasets for a task and explains them in a guide grounded in tool results.",
        model=ledger.model,
        instruction=INSTRUCTION,  # {request} is filled from session state by ADK at run time
        tools=[_hf_toolset()],
        before_agent_callback=_make_resolve_request(),
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        after_model_callback=after_model,
    )


def default_ledger() -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "10")),
    )


# Entry point for `adk web` / `adk run`: the request comes from each chat message.
# (A shared ledger is fine here: it is only a meter and an evidence trail.)
root_agent = build_agent(default_ledger())
