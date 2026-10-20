"""CLI: uv run python -m docs_copilot "How do I declare a dependency in FastAPI?" [-o answer.md] [--metrics m.json]"""

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
from .ledger import is_unanswered, missing_sections, question_from_message, ungrounded_calls, unverified_sources


async def run(question: str) -> tuple[str, object]:
    ledger = default_ledger(question)
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="docs_copilot")
    session = await runner.session_service.create_session(app_name="docs_copilot", user_id="cli")
    answer = ""
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=question)]),
        ):
            for call in event.get_function_calls():
                print(f"  → [{event.author}] {call.name}({_short(call.args)})", file=sys.stderr)
            # Only the answerer's final message is the answer; the researcher hands over via session state.
            if event.author == "answerer" and event.is_final_response() and event.content and event.content.parts:
                answer = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()
    return answer, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Answer a library API question from live docs, with verified sources.")
    parser.add_argument("question")
    parser.add_argument("-o", "--out", type=Path, help="write the answer to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if not question_from_message(args.question):
        print("Ask a full question about a library or API.", file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
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
    unanswered = is_unanswered(answer)
    bad_sources = unverified_sources(answer, ledger)
    bad_calls = ungrounded_calls(answer, ledger)
    if args.metrics:
        args.metrics.write_text(
            json.dumps(
                {
                    "passed": not (unanswered or missing or bad_sources or bad_calls),
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
                        "answered_from_docs": not unanswered,
                        "sections_present": not missing,
                        "sources_verified": not bad_sources,
                        "code_grounded": not bad_calls,
                        "missing_sections": missing,
                        "unverified_sources": bad_sources,
                        "ungrounded_calls": bad_calls,
                    },
                    "output_chars": len(answer),
                },
                indent=2,
            )
        )
    if unanswered:
        print("✗ The retrieved docs did not cover the question (or a docs server failed).", file=sys.stderr)
    if missing:
        print(f"✗ Answer is missing sections: {', '.join(missing)}", file=sys.stderr)
    if bad_sources:
        print(f"✗ Sources not returned by any tool: {', '.join(bad_sources)}", file=sys.stderr)
    if bad_calls:
        print(f"✗ Code calls not found in the retrieved docs: {', '.join(bad_calls)}", file=sys.stderr)
    if unanswered or missing or bad_sources or bad_calls:
        return 1
    print("✓ Sections present, every source was returned by a tool, and every API call appears in the docs.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
