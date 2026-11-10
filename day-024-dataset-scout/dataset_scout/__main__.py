"""CLI: uv run python -m dataset_scout "<task and constraints>" [-o guide.md] [--metrics m.json]"""

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
from .ledger import CITED_LOAD, MIN_REQUEST_CHARS, RunLedger, cited_datasets, unverified_datasets


async def run(request: str) -> RunLedger:
    ledger = default_ledger()
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="dataset_scout")
    session = await runner.session_service.create_session(app_name="dataset_scout", user_id="cli")
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=request)]),
        ):
            for call in event.get_function_calls():
                print(f"  → {call.name}({_short(call.args)})", file=sys.stderr)
            if event.is_final_response() and event.content and event.content.parts:
                ledger.guide = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()
    return ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def run_checks(ledger: RunLedger) -> tuple[dict, list[str]]:
    """Post-run checks on the final guide. Returns (checks, unverified dataset ids)."""
    bad = unverified_datasets(ledger.guide, ledger)
    cited = cited_datasets(ledger.guide)
    checks = {
        "citations_verified": not bad,
        "shortlist_has_2_or_more": len(cited) >= 2,
        "structure_inspected": ledger.structure_calls >= 1,
        "load_snippet_present": bool(CITED_LOAD.search(ledger.guide)),
    }
    return checks, bad


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Find and explain Hugging Face datasets for a task.")
    parser.add_argument("request", help="the task and any constraints")
    parser.add_argument("-o", "--out", type=Path, help="write the guide to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if len(args.request.strip()) < MIN_REQUEST_CHARS:
        print("Describe the task, e.g. 'classify support tickets, English, permissive licence'.", file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    ledger = asyncio.run(run(args.request))
    elapsed = time.perf_counter() - started

    if args.out:
        args.out.write_text(ledger.guide)
    else:
        print(ledger.guide)

    cost = ledger.cost_usd()
    print(
        f"\n— {elapsed:.1f}s · {ledger.model_calls} model calls · {ledger.tool_calls}/{ledger.max_tool_calls} tool calls "
        f"{ledger.calls_by_tool} · {ledger.prompt_tokens:,} in / {ledger.output_tokens:,} out tokens"
        + (f" · ~${cost:.4f}" if cost is not None else ""),
        file=sys.stderr,
    )

    checks, bad = run_checks(ledger)
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
                    "checks": {**checks, "unverified_ids": bad},
                    "output_chars": len(ledger.guide),
                },
                indent=2,
            )
        )
    if bad:
        print(f"✗ Unverified datasets (not returned by any tool): {', '.join(bad)}", file=sys.stderr)
    for name, ok in checks.items():
        if not ok:
            print(f"✗ check failed: {name}", file=sys.stderr)
    if not passed:
        return 1
    print("✓ Every cited dataset was returned by a tool; structure was inspected.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
