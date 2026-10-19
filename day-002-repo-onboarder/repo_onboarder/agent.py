"""Repo Onboarder: turns a public GitHub repo into a new-contributor onboarding guide.

A SequentialAgent runs three stages, each with its own instruction, tools and output_key:

  mapper      read_wiki_structure           -> state["outline"]
  researcher  ask_wiki_question, fetch      -> state["research"]
  writer      (no tools)                    -> state["guide"]

Tools come from two public MCP servers:
  * DeepWiki (remote, streamable HTTP, no auth): AI-generated wiki for any public repo
  * mcp-server-fetch (official reference server, stdio via uvx): raw CONTRIBUTING / README files
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse

from google.adk.agents import Agent, SequentialAgent
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams, StreamableHTTPConnectionParams
from google.genai import types
from mcp import StdioServerParameters

from .ledger import RunLedger, extract_repo

# Pinned so a run today behaves the same as a run next month.
DEEPWIKI_URL = "https://mcp.deepwiki.com/mcp"  # remote, unversioned: the smoke test guards its tool list
FETCH_SERVER = "mcp-server-fetch==2026.10.10"

# Least privilege per stage. read_wiki_contents returns the entire wiki (huge), so it is not exposed.
MAPPER_TOOLS = ["read_wiki_structure"]
RESEARCHER_WIKI_TOOLS = ["ask_wiki_question"]
FETCH_TOOLS = ["fetch"]
DEEPWIKI_TOOLS = MAPPER_TOOLS + RESEARCHER_WIKI_TOOLS

# The target repo lives in session state, set from each user message (see _resolve_repo).
REPO_KEY = "repo_name"
UNAVAILABLE_KEY = "repo_unavailable"
ASK_FOR_REPO = "Which public GitHub repo should I onboard you to? Send `owner/name` or a github.com URL."

# fetch may only read the target repo's own files.
FETCH_HOSTS = {"raw.githubusercontent.com", "github.com"}
FETCH_MAX_LENGTH = 6000

MAPPER_INSTRUCTION = """\
You are the mapper stage of a repo onboarding pipeline for the GitHub repo {repo_name}.
Call read_wiki_structure exactly once. Then output a numbered outline of the 5-7 wiki topics
most useful to a new contributor (architecture, core modules, setup, testing, contributing).
Output only the outline, one topic per line. If the tool fails, output "NO WIKI AVAILABLE".
"""

RESEARCHER_INSTRUCTION = """\
You are the researcher stage of a repo onboarding pipeline for the GitHub repo {repo_name}.

Outline from the previous stage:
{outline}

Call ask_wiki_question 3-4 times (always with repoName="{repo_name}"), one focused question each:
 1. What is this project, who is it for, and how is the code organised (top-level directories and their roles)?
 2. Where does a new reader start: the main entry points and the 3-5 most important modules or files?
 3. How do I set up a development environment, run it, and run the tests?
 4. What are the contribution conventions (branching, style, review) and known pitfalls?
Optionally fetch CONTRIBUTING.md from raw.githubusercontent.com for this repo (at most once).
Do not repeat identical calls. If the outline says NO WIKI AVAILABLE, say so and stop.

Output a compact research digest in Markdown: one section per question, keeping every file path
and URL exactly as the tools returned them. Do not add facts the tools did not return.
"""

WRITER_INSTRUCTION = """\
You are the writer stage of a repo onboarding pipeline for the GitHub repo {repo_name}.
Write the onboarding guide using ONLY this research digest. You have no tools.

Research digest:
{research}

Rules:
- Never invent file paths, commands or URLs. Link to files only as https://github.com/{repo_name}/blob/HEAD/<path>
  when that exact path appears in the digest. If something is not in the digest, write "not covered by the research".
- Be concrete and brief: a new contributor should be able to act on it in their first hour.

Output format (Markdown), with these exact headings:
# Onboarding guide: {repo_name}
## What this project is        (2-3 sentences)
## Architecture map            (table: Area | Path | What lives there)
## Where to start reading      (ordered list of 3-5 files, each with why)
## Local setup                 (commands in a code block)
## How to contribute           (bullets)
## Gotchas                     (bullets)
"""


def _deepwiki_toolset(tool_filter: list[str]) -> McpToolset:
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=DEEPWIKI_URL, timeout=60, sse_read_timeout=120),
        tool_filter=tool_filter,
    )


def _fetch_toolset() -> McpToolset:
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(command="uvx", args=[FETCH_SERVER]),
            timeout=60,  # first run downloads the server
        ),
        tool_filter=FETCH_TOOLS,
    )


def _target_repo(ledger: RunLedger, tool_context) -> str:
    state = getattr(tool_context, "state", None) or {}
    return state.get(REPO_KEY) or ledger.repo


def _text(content) -> str:
    return " ".join(p.text or "" for p in (getattr(content, "parts", None) or []))


def _say(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def _make_pipeline_callbacks(ledger: RunLedger):
    def resolve_repo(callback_context):
        """Runs first on every user message: bind this run to the repo the user named."""
        repo = extract_repo(_text(callback_context.user_content))
        if not repo:
            return _say(ASK_FOR_REPO)  # ends the turn without spending a model call
        callback_context.state[REPO_KEY] = repo
        callback_context.state[UNAVAILABLE_KEY] = ""
        ledger.repo = repo
        return None

    def skip_if_unavailable(callback_context):
        reason = callback_context.state.get(UNAVAILABLE_KEY)
        if not reason:
            return None
        if callback_context.agent_name == "writer":
            return _say(reason)  # the user-facing answer for this run
        return _say("(skipped: no wiki available)")

    return resolve_repo, skip_if_unavailable


def _make_callbacks(ledger: RunLedger):
    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        # Argument guards run before the budget is spent, so a rejected call is free.
        repo = _target_repo(ledger, tool_context)
        if tool.name in DEEPWIKI_TOOLS:
            args["repoName"] = repo  # the pipeline is scoped to one repo; pin it (also fills it in if omitted)
        if tool.name == "fetch":
            host = urlparse(str(args.get("url", ""))).hostname
            path = urlparse(str(args.get("url", ""))).path.lower()
            if host not in FETCH_HOSTS or f"/{repo}/".lower() not in path + "/":
                return {"error": f"fetch is limited to files of {repo} on {sorted(FETCH_HOSTS)}."}
            args["max_length"] = min(int(args.get("max_length") or FETCH_MAX_LENGTH), FETCH_MAX_LENGTH)
        if not ledger.record_tool_call(tool.name):
            # Returning a dict skips the real call; the model sees this instead.
            return {"error": f"Tool budget of {ledger.max_tool_calls} calls reached. Finish with what you have."}
        return None

    def after_tool(tool, args, tool_context, tool_response) -> dict | None:
        text = json.dumps(tool_response, default=str)
        ledger.record_evidence(text)
        if tool.name == "read_wiki_structure" and "Repository not found" in text and tool_context is not None:
            repo = _target_repo(ledger, tool_context)
            tool_context.state[UNAVAILABLE_KEY] = (
                f"DeepWiki has no wiki for **{repo}**. It is either private (DeepWiki only covers public repos) "
                f"or not indexed yet. A public repo can be indexed at https://deepwiki.com/{repo}; then try again."
            )
        return None

    def after_model(callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage:
            ledger.record_usage(usage.prompt_token_count, usage.candidates_token_count)
        return None

    return before_tool, after_tool, after_model


def build_agent(ledger: RunLedger) -> SequentialAgent:
    before_tool, after_tool, after_model = _make_callbacks(ledger)
    resolve_repo, skip_if_unavailable = _make_pipeline_callbacks(ledger)
    common = dict(
        model=ledger.model,
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        after_model_callback=after_model,
    )
    # {repo_name}, {outline} and {research} are filled from session state by ADK at run time.
    mapper = Agent(
        name="mapper",
        description="Lists the wiki topics of the repo.",
        instruction=MAPPER_INSTRUCTION,
        tools=[_deepwiki_toolset(MAPPER_TOOLS)],
        output_key="outline",
        **common,
    )
    researcher = Agent(
        name="researcher",
        description="Asks DeepWiki focused questions and gathers a digest.",
        instruction=RESEARCHER_INSTRUCTION,
        before_agent_callback=skip_if_unavailable,
        tools=[_deepwiki_toolset(RESEARCHER_WIKI_TOOLS), _fetch_toolset()],
        output_key="research",
        **common,
    )
    writer = Agent(
        name="writer",
        description="Writes the onboarding guide from the digest.",
        instruction=WRITER_INSTRUCTION,
        before_agent_callback=skip_if_unavailable,
        output_key="guide",
        **common,
    )
    return SequentialAgent(
        name="repo_onboarder",
        description="Onboarding guide for a public GitHub repo: map, research, write.",
        sub_agents=[mapper, researcher, writer],
        before_agent_callback=resolve_repo,
    )


def default_ledger(repo: str | None = None) -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        repo=repo or "",
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "10")),
    )


# Entry point for `adk web` / `adk run`: the repo comes from each chat message.
root_agent = build_agent(default_ledger())
