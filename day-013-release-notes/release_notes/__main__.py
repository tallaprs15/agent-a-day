"""CLI: uv run python -m release_notes /path/to/repo [--last N | --since DATE [--until DATE]] | --demo [-o notes.md] [--metrics m.json]"""

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
from .demo import make_demo_repo
from .ledger import MAPPERS, check_run, passed


async def run(repo: Path, request: str) -> tuple[str, list, object]:
    ledger = default_ledger()
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="release_notes")
    session = await runner.session_service.create_session(app_name="release_notes", user_id="cli")
    notes = ""
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=request)]),
        ):
            for call in event.get_function_calls():
                print(f"  → {call.name}({_short(call.args)})", file=sys.stderr)
            if event.author.startswith("mapper_") and event.is_final_response():
                print(f"  ⇉ {event.author} done", file=sys.stderr)
            if event.author == "reducer" and event.is_final_response() and event.content and event.content.parts:
                notes = "".join(p.text or "" for p in event.content.parts)
        final = await runner.session_service.get_session(app_name="release_notes", user_id="cli", session_id=session.id)
        maps = [final.state.get(f"map_{i}") for i in range(1, MAPPERS + 1)]
    finally:
        await runner.close()
    return notes, maps, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items() if k != "repo_path")
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Write release notes for a range of git history (map-reduce).")
    parser.add_argument("repo", nargs="?", type=Path, help="path of a local git checkout")
    parser.add_argument("--demo", action="store_true", help="use a bundled, invented 22-commit project history")
    parser.add_argument("--last", type=int, help="notes for the last N commits (default 30)")
    parser.add_argument("--since", help="notes for commits since this ISO date, e.g. 2026-10-01")
    parser.add_argument("--until", help="with --since: stop at this ISO date")
    parser.add_argument("-o", "--out", type=Path, help="write the release notes to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if bool(args.repo) == args.demo:
        parser.error("give a repository path or --demo (not both)")
    if args.last and (args.since or args.until):
        parser.error("use --last or --since/--until, not both")
    repo = make_demo_repo() if args.demo else args.repo.expanduser().resolve()
    if not (repo / ".git").is_dir():
        print(f"{repo} is not the root of a git checkout.", file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    if args.since:
        window = f"since {args.since}" + (f" until {args.until}" if args.until else "")
    else:
        window = f"last {args.last or 30} commits"
    request = f"Release notes for {repo}, {window}"

    started = time.perf_counter()
    notes, maps, ledger = asyncio.run(run(repo, request))
    elapsed = time.perf_counter() - started

    checks = check_run(notes, ledger, maps)
    ok = passed(checks)
    if args.out:
        args.out.write_text(notes.rstrip() + "\n")
    else:
        print(notes)

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
                    "passed": ok,
                    "elapsed_s": round(elapsed, 2),
                    "model": ledger.model,
                    "model_calls": ledger.model_calls,
                    "tool_calls": ledger.tool_calls,
                    "tool_budget": ledger.max_tool_calls,
                    "calls_by_tool": ledger.calls_by_tool,
                    "prompt_tokens": ledger.prompt_tokens,
                    "output_tokens": ledger.output_tokens,
                    "cost_usd": cost,
                    "checks": {k: v for k, v in checks.items() if isinstance(v, (bool, int))},
                    "output_chars": len(notes),
                },
                indent=2,
            )
        )
    if not checks["commits_collected"]:
        print("✗ The agent never collected any commits.", file=sys.stderr)
    if checks["unmapped_shas"]:
        print(f"✗ Map workers skipped commits: {', '.join(checks['unmapped_shas'])}", file=sys.stderr)
    if checks["uncited_shas"]:
        print(f"✗ Notes do not cite commits: {', '.join(checks['uncited_shas'])}", file=sys.stderr)
    if checks["invented_shas"]:
        print(f"✗ Shas that are not in the range: {', '.join(checks['invented_shas'])}", file=sys.stderr)
    if not ok:
        return 1
    print(f"✓ All {checks['commits']} commits classified and cited; none invented.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
