"""CLI: uv run python -m meeting_planner "30 min sync next week, Melbourne and London" [-o plan.md] [--metrics m.json]"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types

from .agent import build_agent, default_ledger
from .ledger import request_from_message
from .plan import MeetingPlan, failed, render_markdown, verify_plan


async def run(request: str) -> tuple[str, object]:
    ledger = default_ledger(request)
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="meeting_planner")
    session = await runner.session_service.create_session(app_name="meeting_planner", user_id="cli")
    raw = ""
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=request)]),
        ):
            for call in event.get_function_calls():
                print(f"  → {call.name}({_short(call.args)})", file=sys.stderr)
            if event.is_final_response() and event.content and event.content.parts:
                raw = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()
    return raw, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Find meeting slots across time zones and return a verified plan.")
    parser.add_argument("request", help='e.g. "30 minutes next week between Melbourne, London and San Francisco"')
    parser.add_argument("-o", "--out", type=Path, help="write the plan (table plus JSON) to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if not request_from_message(args.request):
        print("Tell me who is meeting and when, e.g. '30 minutes next week, Melbourne and London'.", file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    raw, ledger = asyncio.run(run(args.request))
    elapsed = time.perf_counter() - started

    try:
        plan = MeetingPlan.model_validate_json(raw)
        schema_ok = True
    except ValueError as exc:
        plan, schema_ok = MeetingPlan(), False
        print(f"✗ Output is not a valid MeetingPlan: {exc}", file=sys.stderr)

    problems = failed(verify_plan(plan, today=datetime.date.today())) if schema_ok else {}
    asked = bool(plan.needs_clarification)
    unchecked = sorted({p.timezone for p in plan.participants} - ledger.zones_checked) if not asked else []
    checks = {
        "schema_valid": schema_ok,
        "plan_complete": schema_ok and not asked,
        "verified_by_recompute": schema_ok and not problems,
        "zones_checked_by_tool": schema_ok and not unchecked,
    }
    passed = all(checks.values())

    text = render_markdown(plan) + (f"\n```json\n{plan.model_dump_json(indent=2)}\n```\n" if schema_ok else raw)
    if args.out:
        args.out.write_text(text)
    else:
        print(text)

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
                    "checks": {**checks, "problems": problems, "unchecked_zones": unchecked, "slots": len(plan.slots)},
                    "output_chars": len(text),
                },
                indent=2,
            )
        )
    if asked:
        print(f"? The planner needs more information: {plan.needs_clarification}", file=sys.stderr)
    for name, items in problems.items():
        print(f"✗ {name}: {'; '.join(items)}", file=sys.stderr)
    if unchecked:
        print(f"✗ Time zones in the plan that the time server was never asked about: {', '.join(unchecked)}", file=sys.stderr)
    if not passed:
        return 1
    print(f"✓ Valid MeetingPlan; {len(plan.slots)} slot(s) re-computed from the tz database and inside working hours.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
