"""CLI: uv run python -m deep_planner "Launch a personal blog" [-o plan.md] [--metrics m.json]"""

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
from .ledger import (
    graph_problems,
    parse_plan,
    render_markdown,
    request_from_message,
    thinking_problems,
    unchecked_steps,
)


async def run(goal: str) -> tuple[str, object]:
    ledger = default_ledger(goal)
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="deep_planner")
    session = await runner.session_service.create_session(app_name="deep_planner", user_id="cli")
    raw = ""
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=goal)]),
        ):
            for call in event.get_function_calls():
                print(f"  → [{event.author}] {call.name}({_short(call.args)})", file=sys.stderr)
            # Only the writer's final message is the plan; the thinker hands over via session state.
            if event.author == "writer" and event.is_final_response() and event.content and event.content.parts:
                raw = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()
    return raw, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Break a vague goal into a validated plan.")
    parser.add_argument("goal", help='e.g. "Launch a personal blog"')
    parser.add_argument("-o", "--out", type=Path, help="write the plan to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if not request_from_message(args.goal):
        print("Describe the goal in a few words, e.g. 'Launch a personal blog'.", file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    raw, ledger = asyncio.run(run(args.goal))
    elapsed = time.perf_counter() - started

    plan = parse_plan(raw)
    graph = graph_problems(plan) if plan else ["plan did not parse against the schema"]
    unchecked = unchecked_steps(plan) if plan else []
    thinking = thinking_problems(ledger)
    text = render_markdown(plan) if plan and not graph else raw
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
    failed = bool(graph or unchecked or thinking)
    if args.metrics:
        args.metrics.write_text(
            json.dumps(
                {
                    "passed": not failed,
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
                        "graph_valid": not graph,
                        "steps_have_checks": not unchecked,
                        "reasoned_stepwise": not thinking,
                        "graph_problems": graph,
                        "unchecked_steps": unchecked,
                        "thinking_problems": thinking,
                    },
                    "output_chars": len(text),
                },
                indent=2,
            )
        )
    if graph:
        print(f"✗ Plan structure: {'; '.join(graph)}", file=sys.stderr)
    if unchecked:
        print(f"✗ Steps without an acceptance check: {', '.join(unchecked)}", file=sys.stderr)
    if thinking:
        print(f"✗ Reasoning: {'; '.join(thinking)}", file=sys.stderr)
    if failed:
        return 1
    print("✓ Valid dependency graph, every step has an acceptance check, plan came from stepwise reasoning.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
