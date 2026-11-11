"""CLI: uv run python -m api_migrator "Migrate X from 2.1 to 12" [-o plan.md] [--metrics m.json]"""

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
from .ledger import (
    is_unanswered,
    missing_sections,
    request_from_message,
    ungrounded_calls,
    ungrounded_symbols,
    unverified_sources,
)


async def run(request: str) -> tuple[str, object]:
    ledger = default_ledger(request)
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="api_migrator")
    session = await runner.session_service.create_session(app_name="api_migrator", user_id="cli")
    plan = ""
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=request)]),
        ):
            for call in event.get_function_calls():
                print(f"  → [{event.author}] {call.name}({_short(call.args)})", file=sys.stderr)
            # Only the differ's final message is the plan; the researchers hand over via session state.
            if event.author == "differ" and event.is_final_response() and event.content and event.content.parts:
                plan = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()
    return plan, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Write a migration plan between two library versions from their docs.")
    parser.add_argument("request", help='e.g. "Migrate the Azure Storage Blob Python SDK from 2.1 to 12"')
    parser.add_argument("-o", "--out", type=Path, help="write the plan to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if not request_from_message(args.request):
        print("Name a library and both versions, e.g. 'Migrate Pydantic from v1 to v2'.", file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    plan, ledger = asyncio.run(run(args.request))
    elapsed = time.perf_counter() - started

    if args.out:
        args.out.write_text(plan)
    else:
        print(plan)

    cost = ledger.cost_usd()
    print(
        f"\n— {elapsed:.1f}s · {ledger.model_calls} model calls · {ledger.tool_calls}/{ledger.max_tool_calls} tool calls "
        f"{ledger.calls_by_tool} · {ledger.prompt_tokens:,} in / {ledger.output_tokens:,} out tokens"
        + (f" · ~${cost:.4f}" if cost is not None else ""),
        file=sys.stderr,
    )
    missing = missing_sections(plan)
    unanswered = is_unanswered(plan)
    bad_sources = unverified_sources(plan, ledger)
    bad_symbols = ungrounded_symbols(plan, ledger)
    bad_calls = ungrounded_calls(plan, ledger)
    failed = unanswered or missing or bad_sources or bad_symbols or bad_calls
    if args.metrics:
        args.metrics.write_text(
            json.dumps(
                {
                    "passed": not failed,
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
                        "covered_by_docs": not unanswered,
                        "sections_present": not missing,
                        "sources_verified": not bad_sources,
                        "diff_grounded": not bad_symbols,
                        "code_grounded": not bad_calls,
                        "missing_sections": missing,
                        "unverified_sources": bad_sources,
                        "ungrounded_symbols": bad_symbols,
                        "ungrounded_calls": bad_calls,
                    },
                    "output_chars": len(plan),
                },
                indent=2,
            )
        )
    if unanswered:
        print("✗ The retrieved docs did not cover this migration (or a docs server failed).", file=sys.stderr)
    if missing:
        print(f"✗ Plan is missing sections: {', '.join(missing)}", file=sys.stderr)
    if bad_sources:
        print(f"✗ Sources not returned by any tool: {', '.join(bad_sources)}", file=sys.stderr)
    if bad_symbols:
        print(f"✗ Breaking-change APIs not found in the matching docs: {', '.join(bad_symbols)}", file=sys.stderr)
    if bad_calls:
        print(f"✗ Code calls not found in the retrieved docs: {', '.join(bad_calls)}", file=sys.stderr)
    if failed:
        return 1
    print("✓ Sections present, sources returned by a tool, every old API in the old docs, every new API in the new docs.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
