"""Model Router: scores each request's difficulty, then answers on the cheapest model that fits.

  resolve_request (callback)   user message -> state["request"]; asks a question when it is too short
  router (Flash-Lite, no tools) scores reasoning depth, steps and cost of error 0-3 (output_schema)
  dispatcher (custom agent)    code sums the scores and picks a worker; unusable router output fails safe to strong
  lite_worker / strong_worker  same instruction and tools, different model; only the chosen one runs

The model judges difficulty; code does the arithmetic and the routing. Mistakes are asymmetric: sending a hard
request to the cheap model costs quality, sending an easy one to the strong model costs a little money.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncGenerator

from google.adk.agents import Agent, BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters
from pydantic import BaseModel, Field

from .ledger import (
    FETCH_MAX_LENGTH,
    LITE,
    MAX_REQUEST_CHARS,
    MIN_REQUEST_CHARS,
    SCORE_MAX,
    STRONG,
    RunLedger,
    check_fetch_url,
    decide_route,
)

# Pinned so a run today behaves the same as a run next month.
FETCH_SERVER = "mcp-server-fetch==2026.11.4"
FETCH_TOOLS = ["fetch"]  # least privilege: the server exposes only this one tool

REQUEST_KEY = "request"
ASK_FOR_REQUEST = (
    "What would you like answered? Ask a question or paste a URL to read, for example: "
    '"Fetch https://example.com and tell me in two sentences what the page says."'
)

ROUTER_INSTRUCTION = """\
You are the difficulty router for an assistant. You do not answer the request. You only score it.

The request:

{request}

Score each dimension as an integer from 0 to 3:
- reasoning_depth: 0 = lookup or restatement; 1 = simple summary or explanation; 2 = comparison, analysis or
  non-trivial maths or code; 3 = multi-constraint reasoning, proofs, tricky edge cases, or subtle trade-offs.
- multi_step: 0 = one step; 1 = two steps; 2 = several dependent steps; 3 = long chain where each step
  depends on earlier results.
- precision_risk: how costly a subtly wrong answer would be. 0 = casual; 1 = low; 2 = a user may rely on it;
  3 = legal, medical, financial, security or production-critical detail.
Reading a short web page is easy work; do not score it high only because a URL is present.
Give a one-sentence `rationale`.
"""

WORKER_INSTRUCTION = """\
You are a careful, concise assistant. Answer this request:

{request}

If the request contains a URL, call `fetch` to read it and base your answer only on what it returns; never claim
to have read a page you did not fetch. You have a strict tool-call budget and must not repeat identical calls.
If you can answer without tools, do so. If information is missing or the tools fail, say so plainly.
Keep the answer short and in Markdown.
"""


class Assessment(BaseModel):
    reasoning_depth: int = Field(ge=0, le=SCORE_MAX, description="0-3: how much reasoning the answer needs.")
    multi_step: int = Field(ge=0, le=SCORE_MAX, description="0-3: how many dependent steps.")
    precision_risk: int = Field(ge=0, le=SCORE_MAX, description="0-3: cost of a subtly wrong answer.")
    rationale: str = Field(description="One sentence explaining the scores.")


def _text(content) -> str:
    return " ".join(p.text or "" for p in (getattr(content, "parts", None) or []))


def _say(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


# Stdio servers start with a minimal environment. Pass through proxy and CA settings when present so the
# fetch server can reach the web from behind a corporate or sandbox proxy. Nothing else (no API keys) is forwarded.
PASSTHROUGH_ENV = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "UV_SYSTEM_CERTS",
)


def _server_env() -> dict[str, str]:
    return {k: os.environ[k] for k in PASSTHROUGH_ENV if os.getenv(k)}


def _fetch_toolset() -> McpToolset:
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(command="uvx", args=[FETCH_SERVER], env=_server_env() or None),
            timeout=60,  # first run downloads the server
        ),
        tool_filter=FETCH_TOOLS,
    )


def _make_resolve_request(ledger: RunLedger):
    def resolve_request(callback_context):
        """Runs first on every user message: bind this run to the request the user just sent."""
        text = _text(callback_context.user_content).strip()
        if len(text) < MIN_REQUEST_CHARS:
            return _say(ASK_FOR_REQUEST)  # ends the turn without spending a model call
        text = text[:MAX_REQUEST_CHARS]
        callback_context.state[REQUEST_KEY] = text
        callback_context.state["assessment"] = ""  # a stale score must never route a new request
        ledger.request = text
        return None

    return resolve_request


def _make_tool_callbacks(ledger: RunLedger):
    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        if tool.name == "fetch":
            problem = check_fetch_url(args.get("url"))
            if problem:  # rejected before the budget is spent, so a bad call is free
                return {"error": f"fetch refused: {problem}."}
            args["max_length"] = min(int(args.get("max_length") or FETCH_MAX_LENGTH), FETCH_MAX_LENGTH)
        if not ledger.record_tool_call(tool.name):
            # Returning a dict skips the real call; the model sees this instead.
            return {"error": f"Tool budget of {ledger.max_tool_calls} calls reached. Answer with what you have."}
        return None

    def after_tool(tool, args, tool_context, tool_response) -> dict | None:
        text = json.dumps(tool_response, default=str)
        if "Error executing tool" in text or (isinstance(tool_response, dict) and tool_response.get("isError")):
            ledger.tool_errors += 1
        return None

    return before_tool, after_tool


def _make_after_model(ledger: RunLedger, model: str):
    def after_model(callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage:
            ledger.record_usage(model, usage.prompt_token_count, usage.candidates_token_count)
        return None

    return after_model


class RouteDispatcher(BaseAgent):
    """Reads the router's scores from session state and runs exactly one worker."""

    ledger: RunLedger

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        route = decide_route(assessment_dict(ctx.session.state.get("assessment")))
        self.ledger.route, self.ledger.worker = route, route.tier
        worker = next(a for a in self.sub_agents if a.name == f"{route.tier}_worker")
        async for event in worker.run_async(ctx):
            yield event


def _make_capture(ledger: RunLedger):
    def capture(callback_context):
        ledger.answer = str(callback_context.state.get("answer") or "")
        return None

    return capture


def build_agent(ledger: RunLedger) -> SequentialAgent:
    before_tool, after_tool = _make_tool_callbacks(ledger)

    def worker(tier: str, model: str) -> Agent:
        return Agent(
            name=f"{tier}_worker",
            description=f"Answers the request on {model}.",
            model=model,
            instruction=WORKER_INSTRUCTION,
            tools=[_fetch_toolset()],
            output_key="answer",
            before_tool_callback=before_tool,
            after_tool_callback=after_tool,
            after_model_callback=_make_after_model(ledger, model),
            after_agent_callback=_make_capture(ledger),
        )

    router = Agent(
        name="router",
        description="Scores the difficulty of the request; never answers it.",
        model=ledger.lite_model,  # the router is always the cheap model
        instruction=ROUTER_INSTRUCTION,
        output_schema=Assessment,
        output_key="assessment",
        after_model_callback=_make_after_model(ledger, ledger.lite_model),
    )
    dispatcher = RouteDispatcher(
        name="dispatcher",
        description="Runs the lite or strong worker according to the router's scores.",
        sub_agents=[worker(LITE, ledger.lite_model), worker(STRONG, ledger.strong_model)],
        ledger=ledger,
    )
    return SequentialAgent(
        name="model_router",
        description="Scores each request's difficulty and answers it on Flash-Lite or Flash.",
        sub_agents=[router, dispatcher],
        before_agent_callback=_make_resolve_request(ledger),
    )


def default_ledger() -> RunLedger:
    return RunLedger(
        lite_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        strong_model=os.getenv("GEMINI_STRONG_MODEL", "gemini-3.8-flash"),
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "6")),
    )


def assessment_dict(state_value: object) -> dict:
    """The router's output as a dict, whether ADK stored it as a dict or a JSON string."""
    if isinstance(state_value, str):
        try:
            state_value = json.loads(state_value)
        except ValueError:
            return {}
    return state_value if isinstance(state_value, dict) else {}


# Entry point for `adk web` / `adk run`: the request comes from each chat message.
# (A shared ledger is fine here: it is only a meter, and the route is recomputed on every turn.)
root_agent = build_agent(default_ledger())
