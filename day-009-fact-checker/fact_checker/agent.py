"""Fact Checker: verifies claims with verdicts and sources in a generator-critic loop.

  resolve (callback)    user message -> state["claims"] (numbered, one per line)
  refine_loop (LoopAgent, up to MAX_ROUNDS rounds)
    checker             searches DuckDuckGo and reads Wikipedia -> state["verdicts"]
    critic              deterministic checks first (verdict labels, links and quotes that no tool
                        returned); only if those pass, an LLM judges whether the quoted evidence
                        supports each verdict. Approval escalates, which ends the loop.

The checker is the only agent with tools. The critic cannot fetch evidence, it can only reject, and its
feedback goes back to the checker as state["feedback"] for the next round.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from google.adk.agents import Agent, LoopAgent, SequentialAgent
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters
from pydantic import BaseModel, Field

from .ledger import MAX_CLAIM_CHARS, MIN_CLAIM_CHARS, RunLedger, count_claims, report_problems

# Pinned so a run today behaves the same as a run next month.
WIKIPEDIA_SERVER = "wikipedia-mcp==2.0.1"
DUCKDUCKGO_SERVER = "duckduckgo-mcp-server==0.7.0"

# Least privilege: web search finds sources (including Wikipedia titles), get_summary reads one article.
# Wikipedia's own search is left out (its public API rate-limits shared IPs), as are page fetching,
# article sections, links and geo tools.
WIKIPEDIA_TOOLS = ["get_summary"]
DUCKDUCKGO_TOOLS = ["search"]
ALL_TOOLS = sorted(WIKIPEDIA_TOOLS + DUCKDUCKGO_TOOLS)

CLAIMS_KEY = "claims"
ASK_FOR_CLAIMS = (
    "Which claims should I check? Send one or more factual statements, one per line, "
    'for example: "The Great Wall of China is visible from space with the naked eye."'
)
FIRST_PASS_FEEDBACK = "(first pass: no feedback yet)"

SEARCH_MAX_RESULTS = 5
QUERY_MAX = 200
TITLE_MAX = 200

# A successful get_summary result carries a "summary" field (quotes are escaped when it arrives as text).
_HAS_SUMMARY = re.compile(r'\\?"summary\\?"')

_FORWARD_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "UV_SYSTEM_CERTS")

CHECKER_INSTRUCTION = """\
You are a careful fact checker. Check each of these claims, one per line:

{claims}

Feedback from the previous review round (fix every point, keep what was already fine):
{feedback}

Tools: `search` (web search, returns titles, URLs and snippets), `get_summary` (Wikipedia article summary,
argument `title`, the exact article title).
Strict tool budget; never repeat an identical call; if feedback says a source was missing, search for it
rather than writing from memory.

For each claim: search the web, and where a Wikipedia article is relevant read it with `get_summary`.
Prefer primary or reputable sources. Decide a verdict using only what the tools returned in this session:
- Supported: reliable sources confirm the claim as stated.
- Refuted: reliable sources contradict it.
- Mixed: part is true and part is false, or it is true only with a caveat. Say which part.
- Unverifiable: the tools did not return enough to decide. Never guess a verdict.

Output only Markdown in exactly this shape, one block per claim in the same order, claims copied verbatim:

# Fact check
### Claim 1: <the claim>
**Verdict: <Supported|Refuted|Mixed|Unverifiable>**
<one or two sentences of reasoning>
- [<source title>](<exact URL a tool returned>)
  > "<a span of 5 to 20 words copied character for character from that tool result, from the middle of a sentence if that avoids brackets or pronunciations>"
### Claim 2: ...
## Sources
- <bulleted list of every link used>

Rules: Wikipedia links are https://en.wikipedia.org/wiki/<Title_With_Underscores> for the article you
read with `get_summary`. Every link must be a URL a tool returned or a Wikipedia article you read. Every
quote must be copied verbatim from a tool result, never paraphrased. Unverifiable claims need no links.
"""

CRITIC_INSTRUCTION = """\
You are the critic in a generator-critic loop. A checker produced verdicts for these claims:

{claims}

The checker's report:

{verdicts}

Format, link and quote checks have already passed. Judge substance only, using nothing but the report:
1. Does each quoted passage actually support the verdict given for its claim, as the claim is worded?
2. Is any verdict stronger than the quotes justify (for example Supported from a source that only
   partly matches, or Refuted from a source about a different thing)? Qualifiers, dates and numbers in
   the claim must be addressed.
3. Is a claim marked Unverifiable although a quote in the report already decides it?

Approve only if every verdict is justified. If not, set approved to false and list each concrete issue
in `issues`, naming the claim number and what the checker should search for or change. Be strict but
do not invent problems: if everything is sound, approve with an empty `issues` list.
"""


class Critique(BaseModel):
    approved: bool = Field(description="True only if every verdict is justified by the quoted evidence.")
    issues: list[str] = Field(default_factory=list, description="Concrete problems, one per item; empty when approved.")


def _doc_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _FORWARD_ENV if k in os.environ}
    env["FASTMCP_LOG_LEVEL"] = "ERROR"
    return env


def _stdio(package: str, tools: list[str]) -> McpToolset:
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(command="uvx", args=[package], env=_doc_env()),
            timeout=60,  # first run downloads the server
        ),
        tool_filter=tools,
    )


def _text(content) -> str:
    return " ".join(p.text or "" for p in (getattr(content, "parts", None) or []))


def _say(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def _make_resolve_claims():
    def resolve_claims(callback_context):
        """Runs first on every user message: bind this run to the claims the user sent."""
        text = _text(callback_context.user_content).strip()
        if len(text) < MIN_CLAIM_CHARS:
            return _say(ASK_FOR_CLAIMS)  # ends the turn without spending a model call
        text = text[:MAX_CLAIM_CHARS]
        callback_context.state[CLAIMS_KEY] = text
        callback_context.state["feedback"] = FIRST_PASS_FEEDBACK
        callback_context.state["verdicts"] = ""
        return None

    return resolve_claims


def _make_tool_callbacks(ledger: RunLedger):
    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        # Argument guards run before the budget is spent, so a rejected call is free.
        if tool.name == "search":
            args["query"] = str(args.get("query", ""))[:QUERY_MAX]
            args["max_results"] = min(int(args.get("max_results") or SEARCH_MAX_RESULTS), SEARCH_MAX_RESULTS)
        elif tool.name == "get_summary":
            args["title"] = str(args.get("title", ""))[:TITLE_MAX]
        refusal = ledger.record_tool_call(tool.name)
        if refusal:
            # Returning a dict skips the real call; the model sees this instead.
            return {"error": refusal}
        return None

    def after_tool(tool, args, tool_context, tool_response) -> dict | None:
        raw = json.dumps(tool_response, default=str, ensure_ascii=False)
        ledger.record_evidence(raw)
        if tool.name == "get_summary" and args.get("title") and _HAS_SUMMARY.search(raw):
            ledger.record_wikipedia_page(str(args["title"]))
        return None

    def after_model(callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage:
            ledger.record_usage(usage.prompt_token_count, usage.candidates_token_count)
        return None

    return before_tool, after_tool, after_model


def _make_loop_callbacks(ledger: RunLedger):
    def checker_start(callback_context):
        ledger.start_round()
        return None

    def critic_start(callback_context):
        """Deterministic review first: a malformed report is rejected without spending a model call."""
        state = callback_context.state
        claims = state.get(CLAIMS_KEY, "")
        problems = report_problems(state.get("verdicts", "") or "", ledger, expected_claims=count_claims(claims))
        state["mechanical_problems"] = problems
        if problems:
            # Content returned here is saved through the critic's output_schema, so it must be a Critique.
            return _say(Critique(approved=False, issues=problems).model_dump_json())
        return None

    def critic_end(callback_context):
        """Turn the critique into feedback for the next round, or escalate to end the loop."""
        state = callback_context.state
        critique = state.get("critique") or {}
        if isinstance(critique, str):
            try:
                critique = json.loads(critique)
            except ValueError:
                critique = {"approved": False, "issues": [critique]}
        if critique.get("approved") and not state.get("mechanical_problems"):
            callback_context.actions.escalate = True
            state["feedback"] = "Approved."
        else:
            issues = critique.get("issues") or ["The critic did not approve the report."]
            state["feedback"] = "\n".join(f"- {i}" for i in issues)
        return None

    return checker_start, critic_start, critic_end


def build_agent(ledger: RunLedger, max_rounds: int = 3) -> SequentialAgent:
    before_tool, after_tool, after_model = _make_tool_callbacks(ledger)
    checker_start, critic_start, critic_end = _make_loop_callbacks(ledger)
    # {claims}, {feedback}, {verdicts} are filled from session state by ADK at run time.
    checker = Agent(
        name="checker",
        description="Searches the web and Wikipedia and writes a verdict per claim, with quoted evidence.",
        model=ledger.model,
        instruction=CHECKER_INSTRUCTION,
        tools=[_stdio(DUCKDUCKGO_SERVER, DUCKDUCKGO_TOOLS), _stdio(WIKIPEDIA_SERVER, WIKIPEDIA_TOOLS)],
        output_key="verdicts",
        before_agent_callback=checker_start,
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        after_model_callback=after_model,
    )
    critic = Agent(
        name="critic",
        description="Rejects verdicts the quoted evidence does not justify; approves when all are sound.",
        model=ledger.model,
        instruction=CRITIC_INSTRUCTION,
        output_schema=Critique,
        output_key="critique",
        before_agent_callback=critic_start,
        after_agent_callback=critic_end,
        after_model_callback=after_model,
    )
    loop = LoopAgent(
        name="refine_loop",
        description="Checker proposes verdicts, critic reviews them, until approved or out of rounds.",
        sub_agents=[checker, critic],
        max_iterations=max_rounds,
    )
    return SequentialAgent(
        name="fact_checker",
        description="Verifies factual claims with verdicts and quoted sources, refined by a critic loop.",
        sub_agents=[loop],
        before_agent_callback=_make_resolve_claims(),
    )


def default_ledger() -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "14")),
        max_calls_per_round=int(os.getenv("MAX_CALLS_PER_ROUND", "7")),
    )


# Entry point for `adk web` / `adk run`: the claims come from each chat message.
root_agent = build_agent(default_ledger(), max_rounds=int(os.getenv("MAX_ROUNDS", "3")))
