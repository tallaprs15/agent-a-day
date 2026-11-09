"""CLI: uv run python -m a11y_auditor https://example.com/ [-o report.md] [--metrics m.json]"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types

from .agent import build_agent, default_ledger
from .ledger import missing_sections, rubric_problems, unverified_evidence, unverified_pages
from .rubric import in_scope


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


async def run(url: str) -> tuple[str, object]:
    ledger = default_ledger()
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="a11y_auditor")
    session = await runner.session_service.create_session(app_name="a11y_auditor", user_id="cli")
    report = ""
    try:
        async for event in runner.run_async(
            user_id="cli", session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=url)]),
        ):
            for call in event.get_function_calls():
                print(f"  → {call.name}({_short(call.args)})", file=sys.stderr)
            if event.is_final_response() and event.content and event.content.parts and not event.get_function_calls():
                report = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()
    return report, ledger


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Audit a page's accessibility against a fixed WCAG rubric.")
    parser.add_argument("url", help="http(s) URL of the page to audit")
    parser.add_argument("-o", "--out", type=Path, help="write the report to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if not in_scope(args.url, args.url):
        print("Give a full http(s) URL, e.g. https://example.com/", file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    report, ledger = asyncio.run(run(args.url))
    elapsed = time.perf_counter() - started

    if args.out:
        args.out.write_text(report)
    else:
        print(report)

    cost = ledger.cost_usd()
    print(
        f"\n— {elapsed:.1f}s · {ledger.model_calls} model calls · {ledger.tool_calls}/{ledger.max_tool_calls} tool calls "
        f"{ledger.calls_by_tool} · {ledger.prompt_tokens:,} in / {ledger.output_tokens:,} out tokens"
        + (f" · ~${cost:.4f}" if cost is not None else ""),
        file=sys.stderr,
    )
    missing = missing_sections(report)
    rubric_bad = rubric_problems(report) if not missing else []
    bad_evidence = unverified_evidence(report, ledger)
    bad_pages = unverified_pages(report, ledger)
    passed = not (missing or rubric_bad or bad_evidence or bad_pages)
    if args.metrics:
        args.metrics.write_text(
            json.dumps(
                {
                    "passed": passed,
                    "elapsed_s": round(elapsed, 2),
                    "model": ledger.model,
                    "model_calls": ledger.model_calls,
                    "tool_calls": ledger.tool_calls,
                    "tool_budget": ledger.max_tool_calls,
                    "calls_by_tool": ledger.calls_by_tool,
                    "prompt_tokens": ledger.prompt_tokens,
                    "output_tokens": ledger.output_tokens,
                    "cost_usd": cost,
                    "checks": {
                        "sections_present": not missing,
                        "rubric_complete": not missing and not rubric_bad,
                        "evidence_verified": not bad_evidence,
                        "pages_verified": not bad_pages,
                        "missing_sections": missing,
                        "rubric_problems": rubric_bad,
                        "unverified_evidence": bad_evidence,
                        "unverified_pages": bad_pages,
                        "blocked": ledger.blocked,
                    },
                    "output_chars": len(report),
                },
                indent=2,
            )
        )
    if missing:
        print(f"✗ Report is missing sections: {', '.join(missing)}", file=sys.stderr)
    if rubric_bad:
        print(f"✗ Scorecard does not match the rubric: {'; '.join(rubric_bad)}", file=sys.stderr)
    if bad_evidence:
        print(f"✗ Evidence not found in any tool result: {'; '.join(bad_evidence)}", file=sys.stderr)
    if bad_pages:
        print(f"✗ Pages cited but never returned by a tool: {', '.join(bad_pages)}", file=sys.stderr)
    if not passed:
        return 1
    print("✓ Every rubric criterion graded, every failure has a finding, and every quote came from a tool result.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
