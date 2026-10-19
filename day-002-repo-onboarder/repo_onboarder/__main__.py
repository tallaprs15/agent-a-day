"""CLI: uv run python -m repo_onboarder modelcontextprotocol/python-sdk [-o guide.md]"""

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
from .ledger import missing_sections, parse_repo, unverified_links


async def run(repo: str) -> tuple[str, object]:
    ledger = default_ledger(repo)
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="repo_onboarder")
    session = await runner.session_service.create_session(app_name="repo_onboarder", user_id="cli")
    guide = ""
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=f"Onboard me to {repo}")]),
        ):
            for call in event.get_function_calls():
                print(f"  → [{event.author}] {call.name}({_short(call.args)})", file=sys.stderr)
            # Only the writer's final answer is the guide; earlier stages hand over via session state.
            if event.author == "writer" and event.is_final_response() and event.content and event.content.parts:
                guide = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()
    return guide, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Write an onboarding guide for a public GitHub repo.")
    parser.add_argument("repo", help="owner/name or a github.com URL")
    parser.add_argument("-o", "--out", type=Path, help="write the guide to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    try:
        repo = parse_repo(args.repo)
    except ValueError as err:
        print(err, file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    guide, ledger = asyncio.run(run(repo))
    elapsed = time.perf_counter() - started

    if args.out:
        args.out.write_text(guide)
    else:
        print(guide)

    cost = ledger.cost_usd()
    print(
        f"\n— {elapsed:.1f}s · {ledger.model_calls} model calls · {ledger.tool_calls}/{ledger.max_tool_calls} tool calls "
        f"{ledger.calls_by_tool} · {ledger.prompt_tokens:,} in / {ledger.output_tokens:,} out tokens"
        + (f" · ~${cost:.4f}" if cost is not None else ""),
        file=sys.stderr,
    )
    unavailable = guide.startswith("DeepWiki has no wiki for")
    missing = [] if unavailable else missing_sections(guide)
    bad = unverified_links(guide, ledger)
    if args.metrics:
        args.metrics.write_text(
            json.dumps(
                {
                    "passed": not (unavailable or missing or bad),
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
                        "repo_available": not unavailable,
                        "all_sections_present": not (unavailable or missing),
                        "links_verified": not bad,
                        "missing_sections": missing,
                        "unverified_links": bad,
                    },
                    "output_chars": len(guide),
                },
                indent=2,
            )
        )
    if unavailable:
        print(f"✗ No DeepWiki wiki for {repo} (private or not indexed).", file=sys.stderr)
        return 1
    if missing:
        print(f"✗ Guide is missing sections: {', '.join(missing)}", file=sys.stderr)
    if bad:
        print(f"✗ Links not returned by any tool: {', '.join(bad)}", file=sys.stderr)
    if missing or bad:
        return 1
    print("✓ All sections present and every repo link was returned by a tool.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
