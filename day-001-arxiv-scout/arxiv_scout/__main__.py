"""CLI: uv run python -m arxiv_scout "speculative decoding" [-o brief.md] [--metrics m.json]"""

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
from .ledger import unverified_citations


async def run(topic: str) -> tuple[str, object]:
    ledger = default_ledger()
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="arxiv_scout")
    session = await runner.session_service.create_session(app_name="arxiv_scout", user_id="cli")
    brief = ""
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=f"Topic: {topic}")]),
        ):
            if event.get_function_calls():
                for call in event.get_function_calls():
                    print(f"  → {call.name}({_short(call.args)})", file=sys.stderr)
            if event.is_final_response() and event.content and event.content.parts:
                brief = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()
    return brief, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Research a topic on arXiv and write a cited brief.")
    parser.add_argument("topic")
    parser.add_argument("-o", "--out", type=Path, help="write the brief to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_USE_VERTEXAI")):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    brief, ledger = asyncio.run(run(args.topic))
    elapsed = time.perf_counter() - started

    if args.out:
        args.out.write_text(brief)
    else:
        print(brief)

    bad = unverified_citations(brief, ledger)
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
                    "passed": not bad,
                    "elapsed_s": round(elapsed, 2),
                    "model": ledger.model,
                    "model_calls": ledger.model_calls,
                    "tool_calls": ledger.tool_calls,
                    "tool_budget": ledger.max_tool_calls,
                    "calls_by_tool": ledger.calls_by_tool,
                    "prompt_tokens": ledger.prompt_tokens,
                    "output_tokens": ledger.output_tokens,
                    "cost_usd": cost,
                    "checks": {"citations_verified": not bad, "unverified_ids": bad},
                    "output_chars": len(brief),
                },
                indent=2,
            )
        )
    if bad:
        print(f"✗ Unverified citations (not returned by any tool): {', '.join(bad)}", file=sys.stderr)
        return 1
    print("✓ All cited arXiv IDs were returned by a tool.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
