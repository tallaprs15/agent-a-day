"""CLI: uv run python -m hn_digest "top stories" [-o digest.md] [--metrics m.json] [--memory-file F | --fresh]"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types

from .agent import build_agent, default_ledger
from .ledger import (
    is_nothing_new,
    missing_sections,
    repeated_stories,
    request_from_message,
    cited_ids,
    unsaved_stories,
    unverified_links,
)


async def run(request: str) -> tuple[str, object]:
    ledger = default_ledger(request)
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="hn_digest")
    session = await runner.session_service.create_session(app_name="hn_digest", user_id="cli")
    digest = ""
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=request)]),
        ):
            for call in event.get_function_calls():
                print(f"  → {call.name}({_short(call.args)})", file=sys.stderr)
            if event.is_final_response() and event.content and event.content.parts:
                digest = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()
    return digest, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Write a Hacker News digest that skips stories it already covered.")
    parser.add_argument("request", nargs="?", default="top stories", help='what you want, e.g. "5 stories about databases"')
    parser.add_argument("-o", "--out", type=Path, help="write the digest to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    memory = parser.add_mutually_exclusive_group()
    memory.add_argument("--memory-file", type=Path, help="knowledge-graph file (default: .memory/hn-digest.jsonl)")
    memory.add_argument("--fresh", action="store_true", help="start from an empty memory (a temporary file)")
    args = parser.parse_args()

    if not request_from_message(args.request):
        print("Tell me what kind of digest you want, e.g. 'top stories'.", file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2
    if args.fresh:
        args.memory_file = Path(tempfile.mkdtemp(prefix="hn-digest-")) / "memory.jsonl"
    if args.memory_file:
        os.environ["HN_MEMORY_FILE"] = str(args.memory_file.resolve())

    started = time.perf_counter()
    digest, ledger = asyncio.run(run(args.request))
    elapsed = time.perf_counter() - started

    if args.out:
        args.out.write_text(digest)
    else:
        print(digest)

    cost = ledger.cost_usd()
    print(
        f"\n— {elapsed:.1f}s · {ledger.model_calls} model calls · {ledger.tool_calls}/{ledger.max_tool_calls} tool calls "
        f"{ledger.calls_by_tool} · {ledger.prompt_tokens:,} in / {ledger.output_tokens:,} out tokens"
        + (f" · ~${cost:.4f}" if cost is not None else ""),
        file=sys.stderr,
    )
    missing = missing_sections(digest)
    stories = cited_ids(digest)
    has_content = bool(stories) or is_nothing_new(digest)
    bad_links = unverified_links(digest, ledger)
    repeats = repeated_stories(digest, ledger)
    unsaved = unsaved_stories(digest, ledger)
    checks = {
        "sections_present": not missing,
        "has_stories": has_content,
        "links_verified": not bad_links,
        "memory_consulted": ledger.consulted_memory,
        "no_repeats": not repeats,
        "memory_saved": not unsaved,
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
                        "missing_sections": missing,
                        "unverified_links": bad_links,
                        "repeated_stories": repeats,
                        "unsaved_stories": unsaved,
                        "known_before": len(ledger.known_before),
                        "stories_in_digest": len(stories),
                    },
                    "output_chars": len(digest),
                },
                indent=2,
            )
        )
    if missing:
        print(f"✗ Digest is missing sections: {', '.join(missing)}", file=sys.stderr)
    if not has_content:
        print("✗ The digest has no stories and does not say 'Nothing new'.", file=sys.stderr)
    if bad_links:
        print(f"✗ Links not returned by any tool: {', '.join(bad_links)}", file=sys.stderr)
    if not ledger.consulted_memory:
        print("✗ Memory was never consulted before writing.", file=sys.stderr)
    if repeats:
        print(f"✗ Stories memory already held before this run: {', '.join(repeats)}", file=sys.stderr)
    if unsaved:
        print(f"✗ Stories in the digest that were not saved to memory: {', '.join(unsaved)}", file=sys.stderr)
    if not passed:
        return 1
    print(
        f"✓ {len(stories)} new stories, all links returned by a tool, none repeated "
        f"(memory held {len(ledger.known_before)} before), all saved.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
