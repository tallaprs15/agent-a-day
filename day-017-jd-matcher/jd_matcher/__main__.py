"""CLI: uv run python -m jd_matcher "CV: cv.md  Job: job.md" [-o report.md] [--metrics m.json]"""

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
from .ledger import MIN_REQUIREMENTS, RunLedger, parse_request


async def run(request: str) -> RunLedger:
    ledger = default_ledger()
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="jd_matcher")
    session = await runner.session_service.create_session(app_name="jd_matcher", user_id="cli")
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=request)]),
        ):
            for call in event.get_function_calls():
                print(f"  → [{event.author}] {call.name}({_short(call.args)})", file=sys.stderr)
    finally:
        await runner.close()
    return ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Compare a CV to a job description with quoted, verified evidence.")
    parser.add_argument("request", help='which files to compare, e.g. "CV: cv.md  Job: job.md" or "Job: https://..."')
    parser.add_argument("-o", "--out", type=Path, help="write the report to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if parse_request(args.request) is None:
        print('Name a CV and a job, e.g. "CV: cv.md  Job: job.md".', file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    ledger = asyncio.run(run(args.request))
    elapsed = time.perf_counter() - started

    report = ledger.report
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

    checks = {
        "documents_read": ledger.calls_by_tool.get("read_text_file", 0) + ledger.calls_by_tool.get("fetch", 0) >= 2,
        "enough_requirements": ledger.counts.get("requirements", 0) >= MIN_REQUIREMENTS,
        "all_quotes_verified": not ledger.problems,
        "score_computed": ledger.score is not None,
    }
    passed = all(checks.values())
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
                    "checks": {**checks, "problems": ledger.problems, "counts": ledger.counts, "score": ledger.score},
                    "output_chars": len(report),
                },
                indent=2,
            )
        )
    for p in ledger.problems:
        print(f"✗ {p}", file=sys.stderr)
    for name, ok in checks.items():
        if not ok:
            print(f"✗ check failed: {name}", file=sys.stderr)
    if not passed:
        return 1
    print(f"✓ {ledger.counts['requirements']} requirements scored; every quote was found in the CV or job text.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
