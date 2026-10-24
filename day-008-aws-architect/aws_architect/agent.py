"""AWS Architect: reviews an architecture description against AWS documentation.

A SequentialAgent wraps a ParallelAgent fan-out and a merge step:

  resolve (callback)   user message -> state["architecture"]
  reviewers (parallel) cost_reviewer      -> state["cost_review"]
                       security_reviewer  -> state["security_review"]
                       reliability_reviewer -> state["reliability_review"]
  merger               (no tools)         -> state["report"]

Each reviewer owns its own MCP connection to the AWS Documentation server (a public server run over
stdio with uvx), so the three branches search and read docs concurrently.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse

from google.adk.agents import Agent, ParallelAgent, SequentialAgent
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

from .ledger import MIN_ARCHITECTURE_CHARS, RunLedger

# Pinned so a run today behaves the same as a run next month.
AWS_DOCS_SERVER = "awslabs.aws-documentation-mcp-server==1.2.3"

# Least privilege: the server also offers read_documentation (whole pages, paginated),
# recommend and search_table. Reviewers only need to find pages and read named sections.
DOC_TOOLS = ["search_documentation", "read_sections"]

ARCHITECTURE_KEY = "architecture"
ASK_FOR_ARCHITECTURE = (
    "Describe the architecture you want reviewed: the AWS services involved, how data flows between them, "
    "and any scale, availability or compliance requirements. A few sentences or a bulleted list is enough."
)

SEARCH_MAX_RESULTS = 5
SEARCH_PHRASE_MAX = 200
MAX_SECTIONS = 3
DOC_HOST = "docs.aws.amazon.com"

# Environment the stdio server needs to reach the network (e.g. behind a TLS-inspecting proxy).
# The MCP launcher only forwards a minimal environment, so these are passed explicitly.
_FORWARD_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "UV_SYSTEM_CERTS")

REVIEWER_COMMON = """\
You are the {pillar} reviewer in an AWS Well-Architected style review. You see ONLY this architecture:

{architecture}

Process (strict tool budget, do not repeat identical calls):
1. Call search_documentation 2-3 times with specific technical phrases about {focus} for the services
   named above. Use the services the user named; do not assume extra ones.
2. Optionally call read_sections (at most 2 times) on a docs.aws.amazon.com URL returned by search,
   passing the section titles the search suggested.
3. Write your findings.

Rules:
- Every recommendation must be backed by a documentation page returned by your tools in this session.
  Cite it as a Markdown link using the exact URL the tool returned. Never invent URLs or numbers
  (prices, limits, SLAs) that the tools did not return.
- If the description lacks something you need (traffic, data sensitivity, RPO/RTO), list it under
  "Assumptions" instead of guessing.
- Output only Markdown, at most 5 findings, most important first:

### Findings
- **[High|Medium|Low]** <finding about this architecture> — <concrete recommendation> ([<page title>](<url>))
### Assumptions
- <bullets, or "none">
"""

PILLARS = {
    "cost_reviewer": dict(
        pillar="Cost Optimization",
        focus="pricing model, right-sizing, data transfer, storage classes and idle or over-provisioned resources",
        key="cost_review",
    ),
    "security_reviewer": dict(
        pillar="Security",
        focus="IAM least privilege, encryption at rest and in transit, network exposure, logging and secrets",
        key="security_review",
    ),
    "reliability_reviewer": dict(
        pillar="Reliability",
        focus="multi-AZ and failover, backups and recovery, scaling, quotas, health checks and single points of failure",
        key="reliability_review",
    ),
}

MERGER_INSTRUCTION = """\
You are the merge stage of an architecture review. Three reviewers worked in parallel on this architecture:

{architecture}

Their findings (you have no tools, and must not add facts or links that are not below):

## Cost reviewer
{cost_review}

## Security reviewer
{security_review}

## Reliability reviewer
{reliability_review}

Write the final review. Keep every link exactly as written above; drop findings that have no link.
If a reviewer's section is empty or says it could not research, say so under its heading.

Output format (Markdown), with these exact headings:
# Architecture review
## Summary                 (3 bullets: overall posture, biggest risk, quickest win)
## Top priorities          (table: # | Pillar | Finding | Recommendation | Source; the 5 most important across all pillars)
## Cost optimization       (the cost findings, deduplicated)
## Security               (the security findings, deduplicated)
## Reliability             (the reliability findings, deduplicated)
## Trade-offs              (bullets: where two pillars pull in opposite directions, e.g. multi-AZ costs more)
## Sources                 (bulleted list of every link used)
"""


def _doc_toolset() -> McpToolset:
    env = {k: os.environ[k] for k in _FORWARD_ENV if k in os.environ}
    env["FASTMCP_LOG_LEVEL"] = "ERROR"
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(command="uvx", args=[AWS_DOCS_SERVER], env=env),
            timeout=60,  # first run downloads the server
        ),
        tool_filter=DOC_TOOLS,
    )


def _text(content) -> str:
    return " ".join(p.text or "" for p in (getattr(content, "parts", None) or []))


def _say(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def _make_pipeline_callbacks(ledger: RunLedger):
    def resolve_architecture(callback_context):
        """Runs first on every user message: bind this run to the architecture the user described."""
        text = _text(callback_context.user_content).strip()
        if len(text) < MIN_ARCHITECTURE_CHARS:
            return _say(ASK_FOR_ARCHITECTURE)  # ends the turn without spending a model call
        callback_context.state[ARCHITECTURE_KEY] = text
        return None

    return resolve_architecture


def _make_callbacks(ledger: RunLedger):
    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        # Argument guards run before the budget is spent, so a rejected call is free.
        if tool.name == "search_documentation":
            args["search_phrase"] = str(args.get("search_phrase", ""))[:SEARCH_PHRASE_MAX]
            args["limit"] = min(int(args.get("limit") or SEARCH_MAX_RESULTS), SEARCH_MAX_RESULTS)
        if tool.name == "read_sections":
            if urlparse(str(args.get("url", ""))).hostname != DOC_HOST:
                return {"error": f"read_sections is limited to https://{DOC_HOST}/ pages returned by search."}
            titles = args.get("section_titles") or []
            args["section_titles"] = list(titles)[:MAX_SECTIONS]
        agent = getattr(tool_context, "agent_name", "") or ""
        refusal = ledger.record_tool_call(tool.name, agent)
        if refusal:
            # Returning a dict skips the real call; the model sees this instead.
            return {"error": refusal}
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


def build_agent(ledger: RunLedger) -> SequentialAgent:
    before_tool, after_tool, after_model = _make_callbacks(ledger)
    resolve_architecture = _make_pipeline_callbacks(ledger)
    common = dict(
        model=ledger.model,
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        after_model_callback=after_model,
    )
    # {architecture}, {cost_review}, ... are filled from session state by ADK at run time.
    reviewers = [
        Agent(
            name=name,
            description=f"Reviews the architecture for {cfg['pillar']} using AWS documentation.",
            instruction=REVIEWER_COMMON.replace("{pillar}", cfg["pillar"]).replace("{focus}", cfg["focus"]),
            tools=[_doc_toolset()],
            output_key=cfg["key"],
            **common,
        )
        for name, cfg in PILLARS.items()
    ]
    fan_out = ParallelAgent(
        name="reviewers",
        description="Runs the cost, security and reliability reviewers concurrently.",
        sub_agents=reviewers,
    )
    merger = Agent(
        name="merger",
        description="Merges the three reviews into one prioritised report.",
        instruction=MERGER_INSTRUCTION,
        output_key="report",
        **common,
    )
    return SequentialAgent(
        name="aws_architect",
        description="Reviews an AWS architecture on cost, security and reliability, in parallel.",
        sub_agents=[fan_out, merger],
        before_agent_callback=resolve_architecture,
    )


def default_ledger() -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "15")),
        max_calls_per_reviewer=int(os.getenv("MAX_CALLS_PER_REVIEWER", "5")),
    )


# Entry point for `adk web` / `adk run`: the architecture comes from each chat message.
root_agent = build_agent(default_ledger())
