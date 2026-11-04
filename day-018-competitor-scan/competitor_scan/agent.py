"""Competitor Scan: turns a market description into a normalised, evidence-backed competitor table.

Pattern: search -> extract -> normalise, as a SequentialAgent with one job per stage.

  scan (SequentialAgent)
    discover   DuckDuckGo `search` finds candidate products          -> state["candidates"]
    extract    mcp-server-fetch `fetch` reads each vendor's pages    -> state["notes"]
    normalise  no tools; maps the notes onto a fixed schema          -> state["landscape"]
    render     plain Python, no model: drops any row whose URL or quotes no tool returned,
               then prints the table

The model never writes the final table. It fills a typed schema, and code decides which rows survive.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, AsyncGenerator

from google.adk.agents import Agent, BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

from .ledger import Landscape, RunLedger, clean_url, render_table, response_text, split_rows

# Pinned so a run today behaves the same as a run next month.
DUCKDUCKGO_SERVER = "duckduckgo-mcp-server==0.7.0"
FETCH_SERVER = "mcp-server-fetch==2026.10.28"

# Least privilege. DuckDuckGo's server also offers fetch_content and expand_link; the official fetch
# server does the page reading, so only `search` is taken from DuckDuckGo.
DUCKDUCKGO_TOOLS = ["search"]
FETCH_TOOLS = ["fetch"]

MARKET_KEY = "market"
MIN_MARKET_CHARS = 8
MAX_MARKET_CHARS = 300
ASK_FOR_MARKET = (
    "Which market should I scan? Describe it in a sentence, for example: "
    '"open-source feature flag platforms for small engineering teams".'
)

SEARCH_MAX_RESULTS = 10
QUERY_MAX = 200
FETCH_MAX_LENGTH = 6000

_FORWARD_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "UV_SYSTEM_CERTS")

DISCOVER_INSTRUCTION = """\
You are the discovery stage of a competitor scan.

Market to scan: {market}

Run exactly three `search` calls, plain keywords, no search operators and no vendor names:
1. "<market>"  2. "<market> alternatives"  3. "best <market> tools pricing".
Ask for max_results 10. From all results, pick 4 to 6 distinct vendors that compete in this market,
taking each vendor's OWN site (home or pricing page) exactly as a search result lists it. Skip blog posts,
comparison articles, GitHub, app stores and forums as candidate URLs, but use them to learn which
vendors exist: a vendor named in an article whose own URL appeared in some result is a fine candidate.

Output only a list, one product per line, in exactly this shape:
Name | the vendor's own URL, returned by the search | why it belongs in this market

Rules: every URL must be one the search returned. No duplicates. No commentary. If the search finds
nothing relevant, output a single line starting with NONE.
"""

EXTRACT_INSTRUCTION = """\
You are the extraction stage of a competitor scan.

Market: {market}

Candidates found by the discovery stage:
{candidates}

Use `fetch` to read each candidate's own site: its pricing page (usually /pricing) is the best single
page, because it usually says what the product is and what it costs. Fetch every candidate, one page each (work through the list in order and do not stop early), never the
same URL twice, never blog posts, comparison articles or GitHub. If a fetch fails, skip that product
(do not retry it) and move on to the next candidate. You have a strict tool budget: write notes only for products whose page
you actually read.

`fetch` returns the page as numbered passages like "[P12] Plans start at ...". Cite evidence by passage id;
never retype page text.

For each product whose page you read, write one block:
Product: <name>
URL: <exactly the url you fetched>
What it is: <id of the passage that best says what the product is, e.g. P3>
Pricing: <id of the passage that states price or plans, e.g. P9> or NOT FOUND
Open source: yes / no / unknown, with the passage id that shows it
Differentiator: <what sets it apart, in your own words, at most 15 words, from the passages>

A product whose page you did not read gets no block. Code checks that every id exists and belongs to that
page, and deletes rows that fail. If the candidates are NONE, output only the word NONE.
"""

NORMALISE_INSTRUCTION = """\
You are the normalisation stage of a competitor scan. Market: {market}

Raw notes from the extraction stage:
{notes}

Map the notes onto the output schema, one entry per product that has a description passage.
- what_it_is: one sentence of at most 20 words. what_it_is_passage: the passage id from the notes.
- pricing_model: Free, Freemium, Paid, Open source, Contact sales, or Unknown. If the notes say
  NOT FOUND, use Unknown and leave pricing_passage empty. Never infer a price.
- starting_price: the cheapest paid tier as the passage writes it, else "Free" or "Unknown". Any price
  you write must appear in the passage you cite for pricing.
- open_source: Yes or No only when the notes show it, otherwise Unknown.
- Copy passage ids and URLs exactly from the notes. Do not add products, URLs or numbers that are not
  in the notes. Put gaps (unreadable pages, missing prices) in caveats.
If the notes are NONE, return an empty products list and say so in caveats.
"""


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


def _make_resolve_market(ledger: RunLedger):
    def resolve_market(callback_context):
        """Runs first on every user message: bind this run to the market the user described."""
        text = re.sub(r"\s+", " ", _text(callback_context.user_content)).strip()
        if len(text) < MIN_MARKET_CHARS:
            return _say(ASK_FOR_MARKET)  # ends the turn without spending a model call
        ledger.reset()
        state = callback_context.state
        state[MARKET_KEY] = text[:MAX_MARKET_CHARS]
        for key in ("candidates", "notes", "landscape"):
            state[key] = ""
        return None

    return resolve_market


def _is_error(resp, text: str) -> bool:
    return bool(isinstance(resp, dict) and (resp.get("error") or resp.get("isError"))) or text.startswith("Error")


def _make_tool_callbacks(ledger: RunLedger):
    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        # Argument guards run before the budget is spent, so a rejected call is free.
        if tool.name == "search":
            args["query"] = str(args.get("query", ""))[:QUERY_MAX]
            args["max_results"] = min(int(args.get("max_results") or SEARCH_MAX_RESULTS), SEARCH_MAX_RESULTS)
        elif tool.name == "fetch":
            url = str(args.get("url", ""))
            if url.startswith(("http://", "https://")) and not ledger.is_vendor_page(url):
                return {"error": "fetch vendor pages only (a product's own home or pricing page), not articles, code hosts or forums."}
            if not url.startswith(("http://", "https://")) or not ledger.may_fetch(url):
                return {"error": "fetch is limited to pages on sites that the search returned. Pick a URL from the candidates."}
            if ledger.already_requested(url):
                return {"error": "That page was already requested. Use another product's page or write up what you have."}
            ledger.requested_urls.add(clean_url(url))
            args["start_index"] = 0  # one page per product: no paging through long pages
            args["max_length"] = min(int(args.get("max_length") or FETCH_MAX_LENGTH), FETCH_MAX_LENGTH)
            args["raw"] = False
        refusal = ledger.record_tool_call(tool.name)
        if refusal:
            # Returning a dict skips the real call; the model sees this instead.
            return {"error": refusal}
        return None

    def after_tool(tool, args, tool_context, tool_response) -> dict | None:
        text = response_text(tool_response)
        if tool.name == "search":
            ledger.record_search(text)
        elif tool.name == "fetch" and not _is_error(tool_response, text):
            # The model sees numbered passages and cites them by id; it never has to retype evidence.
            url = str(args.get("url", ""))
            passages = ledger.record_page(url, text)
            return {"url": clean_url(url), "passages": [f"[{pid}] {t}" for pid, t in passages]}
        return None

    def after_model(callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage:
            ledger.record_usage(usage.prompt_token_count, usage.candidates_token_count)
        return None

    return before_tool, after_tool, after_model


class Renderer(BaseAgent):
    """Final stage, no model: validate the typed landscape against tool evidence and print the table."""

    ledger: RunLedger

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        raw = ctx.session.state.get("landscape")
        try:
            landscape = Landscape.model_validate(json.loads(raw) if isinstance(raw, str) else raw)
        except Exception:  # empty or malformed output from the model
            landscape = Landscape(market=ctx.session.state.get(MARKET_KEY, ""), products=[],
                                  caveats=["The normalisation stage did not return a usable table."])
        kept, dropped = split_rows(landscape, self.ledger)
        yield Event(author=self.name, content=_say(render_table(landscape, kept, dropped, self.ledger)))

    model_config = {"arbitrary_types_allowed": True}


def build_agent(ledger: RunLedger) -> SequentialAgent:
    before_tool, after_tool, after_model = _make_tool_callbacks(ledger)
    discover = Agent(
        name="discover",
        model=ledger.model,
        description="Finds candidate products with web search.",
        instruction=DISCOVER_INSTRUCTION,
        tools=[_stdio(DUCKDUCKGO_SERVER, DUCKDUCKGO_TOOLS)],
        output_key="candidates",
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        after_model_callback=after_model,
    )
    extract = Agent(
        name="extract",
        model=ledger.model,
        description="Reads vendor pages and copies out evidence.",
        instruction=EXTRACT_INSTRUCTION,
        tools=[_stdio(FETCH_SERVER, FETCH_TOOLS)],
        output_key="notes",
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        after_model_callback=after_model,
    )
    normalise = Agent(
        name="normalise",
        model=ledger.model,
        description="Maps the raw notes onto a fixed schema.",
        instruction=NORMALISE_INSTRUCTION,
        output_schema=Landscape,
        output_key="landscape",
        after_model_callback=after_model,
    )
    render = Renderer(name="render", description="Verifies rows against tool output and renders the table.", ledger=ledger)
    return SequentialAgent(
        name="competitor_scan",
        description="Scans a market and returns a normalised competitor table with evidence.",
        sub_agents=[discover, extract, normalise, render],
        before_agent_callback=_make_resolve_market(ledger),
    )


def default_ledger() -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "14")),
    )


# Entry point for `adk web` / `adk run`. The market comes from the user's message, never from code.
root_agent = build_agent(default_ledger())
