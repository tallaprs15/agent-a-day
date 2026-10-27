"""CLI: uv run python -m perf_auditor https://web.dev [-o report.md] [--metrics m.json]"""

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
from .ledger import report_checks


async def run(url: str) -> tuple[str, object]:
    ledger = default_ledger()
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="perf_auditor")
    session = await runner.session_service.create_session(app_name="perf_auditor", user_id="cli")
    report = ""
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=f"Audit {url}")]),
        ):
            for call in event.get_function_calls() or []:
                print(f"  → {call.name}({_short(call.args)})", file=sys.stderr)
            if event.is_final_response() and event.content and event.content.parts:
                report = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()
    return report, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Record a Chrome performance trace and explain Core Web Vitals.")
    parser.add_argument("url", help="public page to audit")
    parser.add_argument("-o", "--out", type=Path, help="write the report to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_USE_VERTEXAI")):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    report, ledger = asyncio.run(run(args.url))
    elapsed = time.perf_counter() - started

    if args.out:
        args.out.write_text(report)
    else:
        print(report)

    checks = report_checks(report, ledger) if ledger.tool_calls else {"trace_recorded": ["The page was not audited (no tool calls)."]}
    problems = [p for ps in checks.values() for p in ps]
    cost = ledger.cost_usd()
    print(
        f"\n— {elapsed:.1f}s · {ledger.model_calls} model calls · {ledger.tool_calls}/{ledger.max_tool_calls} tool calls "
        f"{ledger.calls_by_tool} · {ledger.prompt_tokens:,} in / {ledger.output_tokens:,} out tokens"
        + (f" · ~${cost:.4f}" if cost is not None else ""),
        file=sys.stderr,
    )
    if args.metrics:
        args.metrics.write_text(
            json.dumps(
                {
                    "passed": not problems,
                    "elapsed_s": round(elapsed, 2),
                    "model": ledger.model,
                    "model_calls": ledger.model_calls,
                    "tool_calls": ledger.tool_calls,
                    "tool_budget": ledger.max_tool_calls,
                    "calls_by_tool": ledger.calls_by_tool,
                    "prompt_tokens": ledger.prompt_tokens,
                    "output_tokens": ledger.output_tokens,
                    "cost_usd": cost,
                    "checks": {name: not ps for name, ps in checks.items()},
                    "output_chars": len(report),
                },
                indent=2,
            )
        )
    if problems:
        print("✗ Report failed checks:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    print("✓ Trace recorded; every metric value and rating checked against tool results.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
