"""CLI: uv run python -m sql_analyst "Which 5 genres have the most tracks?" [-o answer.md] [--metrics m.json]"""

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

from .agent import build_agent, db_path, default_ledger
from .ledger import question_from_message, sql_blocks


async def run(question: str) -> tuple[str, object]:
    ledger = default_ledger(question)
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="sql_analyst")
    session = await runner.session_service.create_session(app_name="sql_analyst", user_id="cli")
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
    text = ", ".join(f"{k}={' '.join(str(v).split())!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Answer a question about a SQLite database with read-only SQL.")
    parser.add_argument("question", help='e.g. "Which 5 genres have the most tracks?"')
    parser.add_argument("-o", "--out", type=Path, help="write the answer to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if not question_from_message(args.question):
        print("Ask a question about the data, e.g. 'Which 5 genres have the most tracks?'.", file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2
    if not db_path().exists():
        print(f"Database not found: {db_path()} (set SQL_DB_PATH).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    answer, ledger = asyncio.run(run(args.question))
    elapsed = time.perf_counter() - started

    if args.out:
        args.out.write_text(answer)
    else:
        print(answer)

    unexecuted = ledger.unexecuted_sql(answer)
    checks = {
        "queried_database": bool(ledger.executed_sql),
        "sql_shown": bool(sql_blocks(answer)),
        "shown_sql_was_executed": bool(sql_blocks(answer)) and not unexecuted,
    }
    passed = all(checks.values())

    cost = ledger.cost_usd()
    print(
        f"\n— {elapsed:.1f}s · {ledger.model_calls} model calls · {ledger.tool_calls}/{ledger.max_tool_calls} tool calls "
        f"{ledger.calls_by_tool} · {ledger.prompt_tokens:,} in / {ledger.output_tokens:,} out tokens"
        + (f" · ~${cost:.4f}" if cost is not None else "")
        + (f" · {len(ledger.rejected_sql)} query rejected by guardrail" if ledger.rejected_sql else ""),
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
                    "checks": {**checks, "rejected_queries": len(ledger.rejected_sql), "unexecuted_sql": len(unexecuted)},
                    "output_chars": len(answer),
                },
                indent=2,
            )
        )
    for name, ok in checks.items():
        if not ok:
            print(f"✗ {name}", file=sys.stderr)
    if unexecuted:
        print(f"✗ {len(unexecuted)} SQL block(s) in the answer were never executed by a tool.", file=sys.stderr)
    if not passed:
        return 1
    print("✓ The answer's SQL is exactly what the database ran, and every query was a guarded SELECT.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
