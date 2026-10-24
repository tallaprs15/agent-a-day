"""CLI: uv run python -m aws_architect "<architecture description>" [-o review.md] [--metrics m.json]"""

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
from .ledger import MIN_ARCHITECTURE_CHARS, missing_sections, unverified_citations


async def run(architecture: str) -> tuple[str, object]:
    ledger = default_ledger()
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="aws_architect")
    session = await runner.session_service.create_session(app_name="aws_architect", user_id="cli")
    report = ""
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=architecture)]),
        ):
            for call in event.get_function_calls():
                print(f"  → [{event.author}] {call.name}({_short(call.args)})", file=sys.stderr)
            # Only the merger's final answer is the report; reviewers hand over via session state.
            if event.author == "merger" and event.is_final_response() and event.content and event.content.parts:
                report = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()
    return report, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Review an AWS architecture on cost, security and reliability.")
    parser.add_argument("architecture", help="a short description of the architecture (services, data flow, requirements)")
    parser.add_argument("-o", "--out", type=Path, help="write the review to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if len(args.architecture.strip()) < MIN_ARCHITECTURE_CHARS:
        print("Describe the architecture in a sentence or two (services, data flow, requirements).", file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    report, ledger = asyncio.run(run(args.architecture))
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
    bad = unverified_citations(report, ledger)
    if args.metrics:
        args.metrics.write_text(
            json.dumps(
                {
                    "passed": not (missing or bad),
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
                        "all_sections_present": not missing,
                        "citations_verified": not bad,
                        "missing_sections": missing,
                        "unverified_links": bad,
                    },
                    "output_chars": len(report),
                },
                indent=2,
            )
        )
    if missing:
        print(f"✗ Review is missing sections: {', '.join(missing)}", file=sys.stderr)
    if bad:
        print(f"✗ AWS doc links not returned by any tool: {', '.join(bad)}", file=sys.stderr)
    if missing or bad:
        return 1
    print("✓ All sections present and every AWS doc link was returned by a tool.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
