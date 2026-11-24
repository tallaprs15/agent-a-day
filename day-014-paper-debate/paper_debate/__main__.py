"""CLI: uv run python -m paper_debate 1706.03762 [-o report.md] [--metrics m.json] [--rounds 2]"""

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
from .ledger import debate_checks, find_arxiv_ids


async def run(paper: str, rounds: int) -> tuple[str, dict, object]:
    ledger = default_ledger()
    runner = InMemoryRunner(agent=build_agent(ledger, rounds), app_name="paper_debate")
    session = await runner.session_service.create_session(app_name="paper_debate", user_id="cli")
    report = ""
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=f"Paper: {paper}")]),
        ):
            for call in event.get_function_calls():
                print(f"  → [{event.author}] {call.name}({_short(call.args)})", file=sys.stderr)
            if event.author in ("advocate", "skeptic", "judge") and event.is_final_response():
                print(f"  ◦ round {ledger.rounds}: {event.author} spoke", file=sys.stderr)
            if event.author == "reporter" and event.content and event.content.parts:
                report = "".join(p.text or "" for p in event.content.parts)
        final = await runner.session_service.get_session(app_name="paper_debate", user_id="cli", session_id=session.id)
        state = dict(final.state)
    finally:
        await runner.close()
    return report, state, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Two agents debate a paper's claims; a judge rules on each.")
    parser.add_argument("paper", help="arXiv ID or link, e.g. 1706.03762")
    parser.add_argument("-o", "--out", type=Path, help="write the report to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    parser.add_argument("--rounds", type=int, default=int(os.getenv("ROUNDS", "2")))
    args = parser.parse_args()

    if not find_arxiv_ids(args.paper):
        print("Give an arXiv ID or link, e.g. 1706.03762.", file=sys.stderr)
        return 2
    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_USE_VERTEXAI")):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    report, state, ledger = asyncio.run(run(args.paper, args.rounds))
    elapsed = time.perf_counter() - started

    if args.out:
        args.out.write_text(report)
    else:
        print(report)

    checks = debate_checks(state, ledger, args.rounds)
    passed = all(checks.values())
    cost = ledger.cost_usd()
    print(
        f"\n— {elapsed:.1f}s · {ledger.rounds} rounds · {ledger.model_calls} model calls · "
        f"{ledger.tool_calls}/{ledger.max_tool_calls} tool calls {ledger.calls_by_tool} · "
        f"{ledger.prompt_tokens:,} in / {ledger.output_tokens:,} out tokens"
        + (f" · ~${cost:.4f}" if cost is not None else ""),
        file=sys.stderr,
    )
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
                    "checks": checks,
                    "output_chars": len(report),
                },
                indent=2,
            )
        )
    for name, ok in checks.items():
        if not ok:
            print(f"✗ check failed: {name}", file=sys.stderr)
    if not passed:
        return 1
    print("✓ Both sides heard, every claim ruled on, every cited arXiv ID returned by a tool.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
