"""CLI: uv run python -m fact_checker "<claim>" ["<claim>" ...] [-o report.md] [--metrics m.json]"""

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
from .ledger import MIN_CLAIM_CHARS, count_claims, report_problems


async def run(claims: str, max_rounds: int) -> tuple[str, object, bool]:
    ledger = default_ledger()
    runner = InMemoryRunner(agent=build_agent(ledger, max_rounds), app_name="fact_checker")
    session = await runner.session_service.create_session(app_name="fact_checker", user_id="cli")
    report, approved = "", False
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=claims)]),
        ):
            for call in event.get_function_calls():
                print(f"  → [{event.author}] {call.name}({_short(call.args)})", file=sys.stderr)
            if event.author == "checker" and event.is_final_response() and event.content and event.content.parts:
                report = "".join(p.text or "" for p in event.content.parts)  # the latest round wins
                print(f"  ◦ round {ledger.rounds}: checker wrote verdicts", file=sys.stderr)
            if event.author == "critic" and event.is_final_response():
                print(f"  ◦ round {ledger.rounds}: critic reviewed", file=sys.stderr)
            if event.actions and event.actions.escalate:
                approved = True  # the critic's after-agent callback escalates only when it approves
                print(f"  ◦ round {ledger.rounds}: critic approved", file=sys.stderr)
    finally:
        await runner.close()
    return report, ledger, approved


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Verify claims with verdicts and sources (generator-critic loop).")
    parser.add_argument("claims", nargs="+", help="one or more claims; each argument is one claim")
    parser.add_argument("-o", "--out", type=Path, help="write the report to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    parser.add_argument("--max-rounds", type=int, default=int(os.getenv("MAX_ROUNDS", "3")))
    args = parser.parse_args()

    claims = "\n".join(c.strip() for c in args.claims if c.strip())
    if len(claims) < MIN_CLAIM_CHARS:
        print("Give at least one claim, e.g. 'The Great Wall of China is visible from space.'", file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    report, ledger, approved = asyncio.run(run(claims, args.max_rounds))
    elapsed = time.perf_counter() - started

    if args.out:
        args.out.write_text(report)
    else:
        print(report)

    cost = ledger.cost_usd()
    print(
        f"\n— {elapsed:.1f}s · {ledger.rounds} rounds · {ledger.model_calls} model calls · "
        f"{ledger.tool_calls}/{ledger.max_tool_calls} tool calls {ledger.calls_by_tool} · "
        f"{ledger.prompt_tokens:,} in / {ledger.output_tokens:,} out tokens"
        + (f" · ~${cost:.4f}" if cost is not None else ""),
        file=sys.stderr,
    )
    problems = report_problems(report, ledger, expected_claims=count_claims(claims))
    if args.metrics:
        args.metrics.write_text(
            json.dumps(
                {
                    "passed": not problems and approved,
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
                        "report_well_formed": not problems,
                        "critic_approved": approved,
                        "rounds": ledger.rounds,
                        "problems": problems,
                    },
                    "output_chars": len(report),
                },
                indent=2,
            )
        )
    for p in problems:
        print(f"✗ {p}", file=sys.stderr)
    if not approved:
        print(f"✗ The critic did not approve the final report after {ledger.rounds} round(s).", file=sys.stderr)
    if problems or not approved:
        return 1
    print(f"✓ Critic approved after {ledger.rounds} round(s); every verdict, link and quote checked against tool results.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
