"""Deep Planner: turns a vague goal into a validated, dependency-ordered plan.

SequentialAgent(thinker, writer), joined by session state:

  thinker  reasons step by step with the Sequential Thinking MCP tool  -> state["analysis"]
  writer   (no tools) turns that analysis into a typed Plan (output_schema) -> state["plan"]

The reasoning tool does no work of its own: it records and echoes thoughts. Its value is that the model
must externalise a numbered, revisable chain of thought before it commits to a plan. Afterwards the ledger
checks the plan deterministically: a real dependency graph (no cycles, no unknown ids), an acceptance
check on every step, and enough reasoning steps. The one MCP server is public and free:
  * @modelcontextprotocol/server-sequential-thinking (stdio via npx)
"""

from __future__ import annotations

import os
from typing import Any

from google.adk.agents import Agent, SequentialAgent
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

from .ledger import MIN_THOUGHTS, Plan, RunLedger, request_from_message

# Pinned so a run today behaves the same as a run next month. (2.x needs Node 24; 1.0.0 runs on Node 18+.)
THINKING_SERVER = "@modelcontextprotocol/server-sequential-thinking@1.0.0"

# The server exposes exactly one tool; the filter is a guard against upstream adding more.
THINKING_TOOLS = ["sequentialthinking"]

GOAL_KEY = "goal"
ASK_FOR_GOAL = "What is the goal? Describe what you want to achieve in a sentence or two, even if it is vague."

MAX_THOUGHTS = 8  # hard ceiling on reasoning steps per run
THOUGHT_MAX_CHARS = 1500

THINKER_INSTRUCTION = """\
You are the thinking stage of a planning pipeline. The user's goal:
{goal}

Reason about it with the sequentialthinking tool. You have no other tools and no web access.
Work in 4 to 6 thoughts, one tool call each, set totalThoughts up front and keep thoughtNumber counting up:
1. Restate the goal precisely and list what is unclear. Pick sensible assumptions; do not ask the user.
2. Define what "done" looks like in observable terms.
3. Decompose the goal into 5 to 9 concrete steps. For each, note which earlier steps it needs.
4. Stress-test the decomposition: missing steps, hidden dependencies, the riskiest step.
5. If thought 4 found a flaw, revise (isRevision=true, revisesThought=<n>) and fix it.
6. Finish with nextThoughtNeeded=false.
Each thought is at most a short paragraph. Do not repeat identical thoughts.

When thinking is complete, reply with a digest of your final conclusions in plain text:
the precise goal, assumptions, the numbered steps with their dependencies, effort guesses
(S under half a day, M 1-2 days, L 3+ days), the riskiest steps and any open questions.
"""

WRITER_INSTRUCTION = """\
You are the writing stage of a planning pipeline. You have no tools.

The user's goal:
{goal}

The thinking stage's conclusions:
{analysis}

Write the plan as the required structured output, using only the conclusions above.
- Step ids are S1, S2, S3 ... in order. depends_on lists ids of earlier steps only; a step with no
  prerequisites has an empty list.
- Every step needs a done_when: an observable check someone else could verify (a file exists, a test
  passes, a decision is written down). "Done" or "TBD" is not a check.
- effort is exactly S, M or L.
- Keep 4 to 12 steps, each titled with an imperative verb. Put unknowns in open_questions, not in steps.
"""


def _thinking_toolset() -> McpToolset:
    from mcp.client.stdio import get_default_environment

    env = get_default_environment()
    env["DISABLE_THOUGHT_LOGGING"] = "true"  # the server would otherwise draw boxes on stderr
    for key in ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                "https_proxy", "http_proxy", "no_proxy"):
        if os.getenv(key):
            env[key] = os.environ[key]
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(command="npx", args=["-y", THINKING_SERVER], env=env),
            timeout=90,  # first run downloads the server
        ),
        tool_filter=THINKING_TOOLS,
    )


def _text(content) -> str:
    return " ".join(p.text or "" for p in (getattr(content, "parts", None) or []))


def _say(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def _make_pipeline_callbacks(ledger: RunLedger):
    def resolve_goal(callback_context):
        """Runs first on every user message: bind this run to the goal the user gave."""
        goal = request_from_message(_text(callback_context.user_content))
        if not goal:
            return _say(ASK_FOR_GOAL)  # ends the turn without spending a model call
        callback_context.state[GOAL_KEY] = goal
        ledger.start(goal)
        return None

    return resolve_goal


def _make_callbacks(ledger: RunLedger):
    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        # Argument caps run before the budget is spent, so a rejected call is free.
        if isinstance(args.get("thought"), str):
            args["thought"] = args["thought"][:THOUGHT_MAX_CHARS]
        total = int(args.get("totalThoughts") or MIN_THOUGHTS)
        args["totalThoughts"] = max(MIN_THOUGHTS, min(total, MAX_THOUGHTS))
        if int(args.get("thoughtNumber") or 1) >= MAX_THOUGHTS:
            args["nextThoughtNeeded"] = False  # ceiling reached: this is the last thought
        if not ledger.record_tool_call(tool.name):
            # Returning a dict skips the real call; the model sees this instead.
            return {"error": f"Tool budget of {ledger.max_tool_calls} calls reached. Reply with your digest now."}
        return None

    def after_tool(tool, args, tool_context, tool_response) -> dict | None:
        if not (isinstance(tool_response, dict) and tool_response.get("isError")):
            ledger.record_thought(args)
        return None

    def after_model(callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage:
            ledger.record_usage(usage.prompt_token_count, usage.candidates_token_count)
        return None

    return before_tool, after_tool, after_model


def build_agent(ledger: RunLedger) -> SequentialAgent:
    before_tool, after_tool, after_model = _make_callbacks(ledger)
    resolve_goal = _make_pipeline_callbacks(ledger)
    common = dict(model=ledger.model, after_model_callback=after_model)
    thinker = Agent(
        name="thinker",
        description="Reasons step by step about the goal with the Sequential Thinking tool.",
        instruction=THINKER_INSTRUCTION,
        tools=[_thinking_toolset()],
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        output_key="analysis",
        **common,
    )
    writer = Agent(
        name="writer",
        description="Writes the typed plan from the thinking stage's conclusions.",
        instruction=WRITER_INSTRUCTION,
        output_schema=Plan,
        output_key="plan",
        **common,
    )
    return SequentialAgent(
        name="deep_planner",
        description="Breaks a vague goal into a validated, dependency-ordered plan.",
        sub_agents=[thinker, writer],
        before_agent_callback=resolve_goal,
    )


def default_ledger(goal: str | None = None) -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        goal=goal or "",
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "10")),
    )


# Entry point for `adk web` / `adk run`: the goal comes from each chat message.
root_agent = build_agent(default_ledger())
