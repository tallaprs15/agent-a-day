"""CLI: uv run python -m competitor_scan "<market>" [-o table.md] [--metrics m.json]"""

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

from .agent import MIN_MARKET_CHARS, build_agent, default_ledger
from .ledger import table_problems


async def run(market: str) -> tuple[str, object]:
    ledger = default_ledger()
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="competitor_scan")
    session = await runner.session_service.create_session(app_name="competitor_scan", user_id="cli")
    report = ""
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=market)]),
        ):
            for call in event.get_function_calls():
                print(f"  → [{event.author}] {call.name}({_short(call.args)})", file=sys.stderr)
            if event.author == "render" and event.content and event.content.parts:
                report = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()
    return report, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Scan a market and write a normalised competitor table.")
    parser.add_argument("market", help="the market to scan, in a sentence")
    parser.add_argument("-o", "--out", type=Path, help="write the table to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if len(args.market.strip()) < MIN_MARKET_CHARS:
        print("Describe the market in a sentence, e.g. 'open-source feature flag platforms'.", file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    report, ledger = asyncio.run(run(args.market))
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
    problems = table_problems(report, ledger)
    left_out = sum(1 for ln in report.splitlines() if ln.startswith("- Not verified"))
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
                    "checks": {
                        "table_verified": not problems,
                        "rows_left_out": left_out,
                        "problems": problems,
                    },
                    "output_chars": len(report),
                },
                indent=2,
            )
        )
    for p in problems:
        print(f"✗ {p}", file=sys.stderr)
    if problems:
        return 1
    print("✓ Every row's URL and quotes were found in tool output.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
