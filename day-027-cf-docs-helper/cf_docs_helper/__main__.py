"""CLI: uv run python -m cf_docs_helper "What are the Workers KV limits?" [-o answer.md] [--metrics m.json]"""

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
from .ledger import cited_links, missing_sections, unverified_links


async def run(question: str) -> tuple[str, object]:
    ledger = default_ledger()
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="cf_docs_helper")
    session = await runner.session_service.create_session(app_name="cf_docs_helper", user_id="cli")
    answer = ""
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=question)]),
        ):
            for call in event.get_function_calls():
                print(f"  → {call.name}({_short(call.args)})", file=sys.stderr)
            if event.is_final_response() and event.content and event.content.parts:
                answer = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()
    return answer, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Answer a Cloudflare question from the official docs.")
    parser.add_argument("question")
    parser.add_argument("-o", "--out", type=Path, help="write the answer to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_USE_VERTEXAI")):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    answer, ledger = asyncio.run(run(args.question))
    elapsed = time.perf_counter() - started

    if args.out:
        args.out.write_text(answer)
    else:
        print(answer)

    cost = ledger.cost_usd()
    print(
        f"\n— {elapsed:.1f}s · {ledger.model_calls} model calls · {ledger.tool_calls}/{ledger.max_tool_calls} tool calls "
        f"{ledger.calls_by_tool} · {ledger.prompt_tokens:,} in / {ledger.output_tokens:,} out tokens"
        + (f" · ~${cost:.4f}" if cost is not None else ""),
        file=sys.stderr,
    )
    missing = missing_sections(answer)
    bad = unverified_links(answer, ledger)
    no_links = not cited_links(answer)
    passed = not (missing or bad or no_links)
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
                        "all_sections_present": not missing,
                        "has_doc_links": not no_links,
                        "links_verified": not bad,
                        "missing_sections": missing,
                        "unverified_links": bad,
                    },
                    "output_chars": len(answer),
                },
                indent=2,
            )
        )
    if missing:
        print(f"✗ Answer is missing sections: {', '.join(missing)}", file=sys.stderr)
    if no_links:
        print("✗ Answer cites no Cloudflare docs pages.", file=sys.stderr)
    if bad:
        print(f"✗ Doc links not returned by the docs server: {', '.join(bad)}", file=sys.stderr)
    if not passed:
        return 1
    print("✓ Sections present and every cited doc link was returned by the docs server.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
