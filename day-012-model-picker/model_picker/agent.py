"""Model Picker: recommends Hugging Face models for a task under constraints, ranked by an explicit rubric.

  resolve_request (callback)  user message -> state["request"]; asks a question when it is too short
  scout (Agent + HF MCP)      searches the Hub, reads details -> state["candidates"] (facts copied from tool results)
  judge (Agent, no tools)     scores task fit and constraint fit 0-5 per candidate (output_schema)
  render (callback)           code adds popularity, licence and freshness from Hub data, computes the weighted
                              totals, sorts, and writes the final report

The model judges; code does the arithmetic and the ordering. Only the scout touches the outside world.
"""

from __future__ import annotations

import json
import os
from typing import Any

from google.adk.agents import Agent, SequentialAgent
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from google.genai import types
from pydantic import BaseModel, Field

from .ledger import MAX_REQUEST_CHARS, MAX_SCORE, MIN_REQUEST_CHARS, RUBRIC, RunLedger, render_report, score_candidates

# The public Hugging Face MCP server is hosted by Hugging Face; its version is reported at initialize (0.4.31 when verified).
HF_MCP_URL = "https://huggingface.co/mcp"

# Least privilege: the server also exposes account inspection and a filesystem-style tool (hf_fs).
# Picking a model needs search and read-only details, nothing else.
HF_TOOLS = ["hub_repo_search", "hub_repo_details"]

REQUEST_KEY = "request"
ASK_FOR_REQUEST = (
    "What do you need a model for? Describe the task and any constraints, for example: "
    '"multilingual sentence embeddings for search, permissive licence, small enough for a laptop CPU".'
)

SEARCH_MAX_LIMIT = 8
DETAILS_MAX_REPOS = 5
QUERY_MAX = 200

SCOUT_INSTRUCTION = """\
You are the scout for a model-selection assistant. The user's request:

{request}

Find 4 to 6 real candidate models on the Hugging Face Hub.
Tools: `hub_repo_search` (arguments: query, repo_types, filters, sort, limit) and `hub_repo_details`
(arguments: repo_ids). Strict tool budget; never repeat an identical call.
1. Search with 1 to 2 focused queries (short keyword queries work best; use `filters` such as a task tag or
   `license:apache-2.0` only when the request implies them, and `sort` by downloads or trending).
2. Optionally call `hub_repo_details` once for the most promising repos to confirm size, licence and updates.

Then output ONLY a Markdown list, one line per candidate, using facts copied from tool results:
- <author/name> | task: <task> | downloads: <n> | licence: <licence or unknown> | notes: <size or architecture hints that bear on the request>

Rules: every candidate must have been returned by a tool in this session. Never invent repo ids or numbers.
If the tools returned nothing useful, say so in one line instead of guessing.
"""

JUDGE_INSTRUCTION = """\
You judge candidate models for this request:

{request}

Candidates found by the scout (facts come from the Hugging Face Hub):

{candidates}

For each candidate give two scores, integers 0 to 5:
- task_fit: how well the model's task, language and domain match what was asked (5 = built for exactly this).
- constraint_fit: how well it meets the stated constraints such as size, speed, hardware or licence
  (5 = clearly satisfies all of them; 3 = unclear from the facts given; 0 = clearly misses).
Set `disqualified` true only if a hard constraint in the request is clearly broken (for example a
non-commercial licence when the request says commercial use). Say why in the rationale.
Write a one or two sentence `rationale` using only the facts above, and a short `caveat` when something is
unknown or risky ("" if none). Do not add up scores or rank: code does that. Use `repo_id` exactly as listed.
Do not add candidates that are not in the list.
"""


class Judgement(BaseModel):
    repo_id: str = Field(description="Exactly as listed by the scout, in author/name form.")
    task_fit: int = Field(ge=0, le=MAX_SCORE, description="0-5: match to the task, language and domain.")
    constraint_fit: int = Field(ge=0, le=MAX_SCORE, description="0-5: how well the stated constraints are met.")
    disqualified: bool = Field(default=False, description="True only if a hard constraint is clearly broken.")
    rationale: str = Field(description="One or two sentences, using only facts the scout listed.")
    caveat: str = Field(default="", description="What is unknown or risky; empty if nothing.")


class Judgements(BaseModel):
    candidates: list[Judgement]


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
        callback_context.state["candidates"] = ""
        return None

    return resolve_request


def _make_tool_callbacks(ledger: RunLedger):
    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        # Argument guards run before the budget is spent, so a rejected call is free.
        if tool.name == "hub_repo_search":
            args["query"] = str(args.get("query", ""))[:QUERY_MAX]
            args["repo_types"] = ["model"]  # this agent recommends models, not datasets or Spaces
            args["limit"] = min(int(args.get("limit") or SEARCH_MAX_LIMIT), SEARCH_MAX_LIMIT)
        elif tool.name == "hub_repo_details":
            args["repo_ids"] = [str(r) for r in (args.get("repo_ids") or [])][:DETAILS_MAX_REPOS]
            args["repo_type"] = "model"
            args["operations"] = ["overview"]  # dataset previews are not needed
        if not ledger.record_tool_call(tool.name):
            # Returning a dict skips the real call; the model sees this instead.
            return {"error": f"Tool budget of {ledger.max_tool_calls} calls reached. Write the candidate list now."}
        return None

    def after_tool(tool, args, tool_context, tool_response) -> dict | None:
        ledger.record_evidence(_response_text(tool_response))
        return None

    def after_model(callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage:
            ledger.record_usage(usage.prompt_token_count, usage.candidates_token_count)
        return None

    return before_tool, after_tool, after_model


def _make_render(ledger: RunLedger):
    def render(callback_context):
        """After the judge: score with code, rank, and replace the JSON with the report."""
        state = callback_context.state
        raw = state.get("judgement") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        judged = [c if isinstance(c, dict) else dict(c) for c in raw.get("candidates", [])]
        ranked, disqualified, problems = score_candidates(judged, ledger)
        ledger.ranked, ledger.disqualified, ledger.problems = ranked, disqualified, problems
        ledger.report = render_report(state.get(REQUEST_KEY, ""), ranked, disqualified)
        return _say(ledger.report)

    return render


def build_agent(ledger: RunLedger) -> SequentialAgent:
    before_tool, after_tool, after_model = _make_tool_callbacks(ledger)
    # {request} and {candidates} are filled from session state by ADK at run time.
    scout = Agent(
        name="scout",
        description="Searches the Hugging Face Hub and lists candidate models with facts from the tool results.",
        model=ledger.model,
        instruction=SCOUT_INSTRUCTION,
        tools=[_hf_toolset()],
        output_key="candidates",
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        after_model_callback=after_model,
    )
    judge = Agent(
        name="judge",
        description="Scores task fit and constraint fit for each candidate; code computes the rest of the rubric.",
        model=ledger.model,
        instruction=JUDGE_INSTRUCTION,
        output_schema=Judgements,
        output_key="judgement",
        after_model_callback=after_model,
        after_agent_callback=_make_render(ledger),
    )
    return SequentialAgent(
        name="model_picker",
        description="Recommends Hugging Face models for a task under constraints, ranked by an explicit scoring rubric.",
        sub_agents=[scout, judge],
        before_agent_callback=_make_resolve_request(),
    )


def default_ledger() -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "10")),
    )


# Entry point for `adk web` / `adk run`: the request comes from each chat message.
# (A shared ledger is fine here: it is only a meter, and the report is rebuilt on every turn.)
root_agent = build_agent(default_ledger())
