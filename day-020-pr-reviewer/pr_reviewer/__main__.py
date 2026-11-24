"""CLI: uv run python -m pr_reviewer https://github.com/owner/repo/pull/123 [-o review.md] [--metrics m.json]"""

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
from .ledger import parse_pr_url, unseen_files

REQUIRED_SECTIONS = ("## Summary", "## Findings", "## Verdict")


async def run(pr_url: str) -> tuple[str, object]:
    ledger = default_ledger()
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="pr_reviewer")
    session = await runner.session_service.create_session(app_name="pr_reviewer", user_id="cli")
    review = ""
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=f"Review {pr_url}")]),
        ):
            for call in event.get_function_calls() or []:
                print(f"  → {call.name}({_short(call.args)})", file=sys.stderr)
            if event.is_final_response() and event.content and event.content.parts:
                review = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()
    return review, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Read-only review of a public GitHub pull request.")
    parser.add_argument("pr_url", help="https://github.com/owner/repo/pull/123")
    parser.add_argument("-o", "--out", type=Path, help="write the review to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if parse_pr_url(args.pr_url) is None:
        print("Expected a pull request link like https://github.com/owner/repo/pull/123", file=sys.stderr)
        return 2
    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_USE_VERTEXAI")):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    review, ledger = asyncio.run(run(args.pr_url))
    elapsed = time.perf_counter() - started

    if args.out:
        args.out.write_text(review)
    else:
        print(review)

    bad = unseen_files(review, ledger)
    checks = {
        "diff_read": bool(ledger.seen_files),
        "sections_present": all(s in review for s in REQUIRED_SECTIONS),
        "files_verified": not bad,
    }
    passed = all(checks.values())
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
                    "checks": {**checks, "unseen_files": bad},
                    "output_chars": len(review),
                },
                indent=2,
            )
        )
    if not passed:
        failed = [k for k, ok in checks.items() if not ok]
        print(f"✗ Checks failed: {', '.join(failed)}" + (f" (files not in the diff: {', '.join(bad)})" if bad else ""), file=sys.stderr)
        return 1
    print("✓ Diff was read, sections are present, and every cited file appears in the diff.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
