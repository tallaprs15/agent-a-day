"""CLI: uv run python -m kg_builder URL [URL ...] [-o report.md] [--metrics m.json]"""

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

from .agent import build_agent, default_ledger, memory_file
from .ledger import MAX_URLS, RunLedger, urls_from_message

MIN_ENTITIES = 3


async def run(message: str) -> RunLedger:
    ledger = default_ledger()
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="kg_builder")
    session = await runner.session_service.create_session(app_name="kg_builder", user_id="cli")
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=message)]),
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
    parser = argparse.ArgumentParser(description="Build a grounded knowledge graph from web pages.")
    parser.add_argument("urls", nargs="+", help=f"up to {MAX_URLS} https links")
    parser.add_argument("-o", "--out", type=Path, help="write the report to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    urls = urls_from_message(" ".join(args.urls))
    if not urls:
        print("Give at least one https link.", file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    print(f"Graph file: {memory_file()}", file=sys.stderr)
    started = time.perf_counter()
    ledger = asyncio.run(run(" ".join(urls)))
    elapsed = time.perf_counter() - started

    if args.out:
        args.out.write_text(ledger.report)
    else:
        print(ledger.report)

    cost = ledger.cost_usd()
    print(
        f"\n— {elapsed:.1f}s · {ledger.model_calls} model calls · {ledger.tool_calls}/{ledger.max_tool_calls} tool calls "
        f"{ledger.calls_by_tool} · {ledger.prompt_tokens:,} in / {ledger.output_tokens:,} out tokens"
        + (f" · ~${cost:.4f}" if cost is not None else ""),
        file=sys.stderr,
    )

    missing_entities, missing_relations = ledger.missing_entities(), ledger.missing_relations()
    checks = {
        "pages_fetched": set(urls) == ledger.fetched,
        "graph_extracted": len(ledger.entities) >= MIN_ENTITIES and len(ledger.relations) >= 1,
        "read_back": ledger.verified,
        "entities_persisted": ledger.verified and not missing_entities,
        "relations_persisted": ledger.verified and not missing_relations,
        "no_writes_outside_plan": not ledger.gate_blocks,
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
                    "checks": {
                        **checks,
                        "entities": len(ledger.entities),
                        "relations": len(ledger.relations),
                        "dropped": len(ledger.dropped),
                        "gate_blocks": ledger.gate_blocks,
                        "missing_entities": missing_entities,
                        "missing_relations": missing_relations,
                    },
                    "output_chars": len(ledger.report),
                },
                indent=2,
            )
        )
    for name, ok in checks.items():
        if not ok:
            print(f"✗ check failed: {name}", file=sys.stderr)
    for block in ledger.gate_blocks:
        print(f"✗ blocked write: {block}", file=sys.stderr)
    if not passed:
        return 1
    print(
        f"✓ {len(ledger.entities)} entities and {len(ledger.relations)} relations, each backed by a quote from a fetched page, "
        "written and read back from the memory server.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
