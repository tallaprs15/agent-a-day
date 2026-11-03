"""JD Matcher: compares a CV to a job description and returns a typed, evidence-checked gap analysis.

  resolve_request (callback)  user message -> state: which CV file and which job (file or URL); asks when unclear
  reader (Agent + 2 MCP)      reads the CV from a sandboxed folder (Filesystem MCP) and the job (file or Fetch MCP);
                              after_tool stores the raw texts in session state, the model never retypes them
  matcher (Agent, no tools)   fills a Pydantic schema (output_schema): requirements, status, quoted evidence, bullets
  render (callback)           code verifies every quote against the stored texts, scores, and writes the report

The model judges; code checks the quotes, does the arithmetic and writes the verdict.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from google.adk.agents import Agent, SequentialAgent
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters
from pydantic import BaseModel, Field

from .ledger import (
    ASK_FOR_INPUTS,
    MAX_DOC_CHARS,
    RunLedger,
    check_analysis,
    fit_score,
    parse_request,
    render_report,
    resolve_in_sandbox,
)

# Pinned so a run today behaves the same as a run next month.
FILESYSTEM_SERVER = "@modelcontextprotocol/server-filesystem@2.1.0"  # needs Node 24+
FETCH_SERVER = "mcp-server-fetch==2026.10.28"

# Least privilege: the filesystem server can also write, move and edit files. Matching needs one read tool.
FILESYSTEM_TOOLS = ["read_text_file"]
FETCH_TOOLS = ["fetch"]

# The only folder the agent may read from. Override with JD_SANDBOX.
DEFAULT_SANDBOX = Path(__file__).resolve().parent.parent / "samples"

CV_REF, JOB_REF = "cv_ref", "job_ref"
READ_LINES_MAX = 400
FETCH_MAX_CHARS = 12_000

READER_INSTRUCTION = """\
You fetch two documents, nothing else.
1. Call `read_text_file` with path "{cv_ref}" to read the CV.
2. Read the job description: if "{job_ref}" starts with http, call `fetch` with that exact url;
   otherwise call `read_text_file` with path "{job_ref}".
Make each call exactly once, with exactly these values. The system stores the texts for you.
When both calls are done, reply with the single word: done. If a call fails, reply with one line saying which document failed and why.
"""

MATCHER_INSTRUCTION = """\
You compare a CV with a job description. Use ONLY the two documents below.

=== JOB DESCRIPTION ===
{jd_text}

=== CV ===
{cv_text}
=== END ===

Fill the schema:
- requirements: the 5 to 10 most important requirements of the job (skills, experience, qualifications).
  Mark each importance "must" (stated as required) or "nice" (preferred or bonus).
  `jd_quote` is a short phrase copied exactly from the job description, 8 to 25 words, no rewording.
- For each requirement set `status`: "met" (clear evidence in the CV), "partial" (related but weaker, shorter,
  or in a different context) or "gap" (nothing in the CV supports it).
  For met and partial, `cv_evidence` is a phrase copied exactly from the CV, no rewording, no joining of lines.
  For gap, leave `cv_evidence` empty. Never claim a skill the CV does not state; adjacent skills are "partial", not "met".
- tailored_bullets: 3 to 5 CV bullet points reworded to speak to this job's language. Each `cv_source` is the exact
  CV line it rewords. A bullet may reorder and rephrase; it must NOT add facts, tools, employers, or numbers
  that are not in that CV line.
- fit_summary: two sentences, plain, no scores (code computes the score).
"""


class Requirement(BaseModel):
    requirement: str = Field(description="The requirement in a few words.")
    jd_quote: str = Field(description="Exact phrase copied from the job description that states it.")
    importance: Literal["must", "nice"]
    status: Literal["met", "partial", "gap"]
    cv_evidence: str = Field(default="", description="Exact phrase copied from the CV; empty for a gap.")
    note: str = Field(default="", description="Optional: what is missing or only partly covered.")


class TailoredBullet(BaseModel):
    bullet: str = Field(description="CV line reworded for this job, using only facts in the source line.")
    cv_source: str = Field(description="The exact CV line it was reworded from.")


class Analysis(BaseModel):
    role_title: str = Field(description="Job title from the job description.")
    fit_summary: str = Field(description="Two plain sentences, no scores.")
    requirements: list[Requirement]
    tailored_bullets: list[TailoredBullet]


def sandbox_dir() -> Path:
    return Path(os.getenv("JD_SANDBOX") or DEFAULT_SANDBOX).resolve()


def _text(content) -> str:
    return " ".join(p.text or "" for p in (getattr(content, "parts", None) or []))


def _say(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def _response_text(response) -> str:
    """The text an MCP tool returned (content parts, else structured content, else the raw value)."""
    if isinstance(response, dict):
        parts = [c.get("text", "") for c in response.get("content") or [] if isinstance(c, dict)]
        if parts:
            return "\n".join(parts)
        structured = response.get("structuredContent")
        if isinstance(structured, dict) and isinstance(structured.get("content"), str):
            return structured["content"]
    return response if isinstance(response, str) else json.dumps(response, default=str, ensure_ascii=False)


def _filesystem_toolset() -> McpToolset:
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(command="npx", args=["-y", FILESYSTEM_SERVER, str(sandbox_dir())]),
            timeout=90,  # first run downloads the server
        ),
        tool_filter=FILESYSTEM_TOOLS,
    )


def _fetch_toolset() -> McpToolset:
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(command="uvx", args=[FETCH_SERVER]),
            timeout=60,
        ),
        tool_filter=FETCH_TOOLS,
    )


def _make_resolve_request():
    def resolve_request(callback_context):
        """Runs first on every user message: bind this run to the CV and job the user just named."""
        refs = parse_request(_text(callback_context.user_content))
        if refs is None:
            return _say(ASK_FOR_INPUTS)  # ends the turn without spending a model call
        cv_ref, job_ref = refs
        if resolve_in_sandbox(cv_ref, sandbox_dir()) is None:
            return _say(f"The CV must be a .md or .txt file inside the sandbox folder ({sandbox_dir().name}/). {ASK_FOR_INPUTS}")
        if not job_ref.startswith(("http://", "https://")) and resolve_in_sandbox(job_ref, sandbox_dir()) is None:
            return _say(f"The job must be a .md or .txt file inside the sandbox folder, or an http(s) URL. {ASK_FOR_INPUTS}")
        state = callback_context.state
        state[CV_REF], state[JOB_REF] = cv_ref, job_ref
        state["cv_text"] = state["jd_text"] = ""
        return None

    return resolve_request


def _make_tool_callbacks(ledger: RunLedger):
    def before_tool(tool, args: dict[str, Any], tool_context) -> dict | None:
        # Argument guards run before the budget is spent, so a rejected call is free.
        state = tool_context.state
        if tool.name == "read_text_file":
            wanted = {resolve_in_sandbox(state.get(r, ""), sandbox_dir()) for r in (CV_REF, JOB_REF)} - {None}
            path = resolve_in_sandbox(str(args.get("path", "")), sandbox_dir())
            if path is None or path not in wanted:
                return {"error": "Only the CV and job files named by the user can be read."}
            args["path"] = str(path)
            args.pop("tail", None)
            args["head"] = min(int(args.get("head") or READ_LINES_MAX), READ_LINES_MAX)
        elif tool.name == "fetch":
            if str(args.get("url", "")) != state.get(JOB_REF):
                return {"error": "Only the job URL named by the user can be fetched."}
            args["max_length"] = min(int(args.get("max_length") or FETCH_MAX_CHARS), FETCH_MAX_CHARS)
            args["raw"] = False
        if not ledger.record_tool_call(tool.name):
            # Returning a dict skips the real call; the model sees this instead.
            return {"error": f"Tool budget of {ledger.max_tool_calls} calls reached. Stop and say which document is missing."}
        return None

    def after_tool(tool, args, tool_context, tool_response) -> dict | None:
        """Keep the raw text in session state. The matcher reads it from there, not from the model's memory."""
        state = tool_context.state
        text = _response_text(tool_response)[:MAX_DOC_CHARS]
        is_cv = False
        if tool.name == "read_text_file":
            cv_path = resolve_in_sandbox(state.get(CV_REF, ""), sandbox_dir())
            is_cv = resolve_in_sandbox(str(args.get("path", "")), sandbox_dir()) == cv_path
        state["cv_text" if is_cv else "jd_text"] = text
        return None

    def after_model(callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage:
            ledger.record_usage(usage.prompt_token_count, usage.candidates_token_count)
        return None

    return before_tool, after_tool, after_model


def _make_render(ledger: RunLedger):
    def render(callback_context):
        """After the matcher: verify every quote, score with code, replace the JSON with the report."""
        state = callback_context.state
        raw = state.get("analysis") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        reqs, bullets, problems = check_analysis(raw, state.get("cv_text", ""), state.get("jd_text", ""))
        ledger.problems = problems
        ledger.counts = {s: sum(r.get("status") == s for r in reqs) for s in ("met", "partial", "gap")}
        ledger.counts["requirements"] = len(reqs)
        ledger.counts["bullets"] = len(bullets)
        ledger.score = fit_score(reqs)
        ledger.report = render_report(raw.get("role_title", ""), raw.get("fit_summary", ""), reqs, bullets, ledger.score, problems)
        return _say(ledger.report)

    return render


def build_agent(ledger: RunLedger) -> SequentialAgent:
    before_tool, after_tool, after_model = _make_tool_callbacks(ledger)
    # {cv_ref}, {job_ref}, {cv_text} and {jd_text} are filled from session state by ADK at run time.
    reader = Agent(
        name="reader",
        description="Reads the CV from the sandbox folder and the job description from a file or URL.",
        model=ledger.model,
        instruction=READER_INSTRUCTION,
        tools=[_filesystem_toolset(), _fetch_toolset()],
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        after_model_callback=after_model,
    )
    matcher = Agent(
        name="matcher",
        description="Compares the two documents and fills a typed analysis; every claim carries a quote.",
        model=ledger.model,
        instruction=MATCHER_INSTRUCTION,
        output_schema=Analysis,
        output_key="analysis",
        after_model_callback=after_model,
        after_agent_callback=_make_render(ledger),
    )
    return SequentialAgent(
        name="jd_matcher",
        description="Compares a CV to a job description: weighted fit score, quoted evidence and tailored bullets.",
        sub_agents=[reader, matcher],
        before_agent_callback=_make_resolve_request(),
    )


def default_ledger() -> RunLedger:
    return RunLedger(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "6")),
    )


# Entry point for `adk web` / `adk run`: the CV and job come from each chat message.
# (A shared ledger is fine here: it is only a meter, and the report is rebuilt on every turn.)
root_agent = build_agent(default_ledger())
