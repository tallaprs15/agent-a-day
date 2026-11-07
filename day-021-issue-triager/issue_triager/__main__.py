"""CLI: uv run python -m issue_triager https://github.com/owner/repo/issues/123 [-o triage.md] [--metrics m.json]"""

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
from .ledger import parse_issue_url, validate

REQUIRED_SECTIONS = ("## Classification", "## Labels", "## Possible duplicates", "## Suggested owners", "## Next step")


async def run(issue_url: str) -> tuple[str, object]:
    ledger = default_ledger()
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="issue_triager")
    session = await runner.session_service.create_session(app_name="issue_triager", user_id="cli")
    triage = ""
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=f"Triage {issue_url}")]),
        ):
            for call in event.get_function_calls() or []:
                print(f"  → {call.name}({_short(call.args)})", file=sys.stderr)
            if event.is_final_response() and event.content and event.content.parts:
                triage = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()
    return triage, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 110 else text[:107] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Read-only triage of a public GitHub issue: labels, duplicates, owners.")
    parser.add_argument("issue_url", help="https://github.com/owner/repo/issues/123")
    parser.add_argument("-o", "--out", type=Path, help="write the triage to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    ref = parse_issue_url(args.issue_url)
    if ref is None:
        print("Expected an issue link like https://github.com/owner/repo/issues/123", file=sys.stderr)
        return 2
    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_USE_VERTEXAI")):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    triage, ledger = asyncio.run(run(args.issue_url))
    elapsed = time.perf_counter() - started

    if args.out:
        args.out.write_text(triage)
    else:
        print(triage)

    problems = validate(triage, ledger, ref[2])
    checks = {
        "issue_read": ledger.target_seen,
        "labels_listed": ledger.labels_listed,
        "sections_present": all(s in triage for s in REQUIRED_SECTIONS),
        "labels_exist": not problems["unknown_labels"],
        "duplicates_grounded": not problems["unseen_duplicates"],
        "owners_grounded": not problems["unseen_owners"],
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
                    "checks": {**checks, **{k: v for k, v in problems.items() if v}},
                    "output_chars": len(triage),
                },
                indent=2,
            )
        )
    if not passed:
        failed = [k for k, ok in checks.items() if not ok]
        detail = "; ".join(f"{k}: {v}" for k, v in problems.items() if v)
        print(f"✗ Checks failed: {', '.join(failed)}" + (f" ({detail})" if detail else ""), file=sys.stderr)
        return 1
    print("✓ Issue and labels were read; every label, duplicate and owner named in the triage came from tool results.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
