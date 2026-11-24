"""CLI: uv run python -m model_router "<question or URL task>" [-o answer.md] [--metrics m.json]"""

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
from .ledger import LITE, MIN_REQUEST_CHARS, STRONG, URL, RunLedger, decide_route


async def run(request: str) -> RunLedger:
    ledger = default_ledger()
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="model_router")
    session = await runner.session_service.create_session(app_name="model_router", user_id="cli")
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=request)]),
        ):
            for call in event.get_function_calls():
                print(f"  → [{event.author}] {call.name}({_short(call.args)})", file=sys.stderr)
        state = (await runner.session_service.get_session(app_name="model_router", user_id="cli", session_id=session.id)).state
        ledger.answer = str(state.get("answer") or "")
    finally:
        await runner.close()
    return ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Answer a request on the cheapest Gemini model that fits its difficulty.")
    parser.add_argument("request", help="a question, or a task that mentions a URL to read")
    parser.add_argument("-o", "--out", type=Path, help="write the answer to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if len(args.request.strip()) < MIN_REQUEST_CHARS:
        print("Ask a question or describe a task.", file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    ledger = asyncio.run(run(args.request))
    elapsed = time.perf_counter() - started

    answer = ledger.answer
    if args.out:
        args.out.write_text(answer)
    else:
        print(answer)

    route = ledger.route
    cost = ledger.cost_usd()
    route_line = (
        f"route: {route.tier} ({ledger.model}) · difficulty {route.total if route.total is not None else 'n/a'}/9 {route.scores}"
        if route
        else "route: none"
    )
    meter = (
        f"— {elapsed:.1f}s · {ledger.model_calls} model calls {ledger.calls_by_model} · "
        f"{ledger.tool_calls}/{ledger.max_tool_calls} tool calls {ledger.calls_by_tool} ({ledger.tool_errors} failed) · "
        f"{ledger.prompt_tokens:,} in / {ledger.output_tokens:,} out tokens"
        + (f" · ~${cost:.4f}" if cost is not None else "")
    )
    print(f"\n{route_line}\n{meter}", file=sys.stderr)

    expected = decide_route(_assessment_from_route(route)).tier if route and route.total is not None else STRONG
    checks = {
        "route_recorded": route is not None,
        "router_output_usable": bool(route and route.total is not None),
        "worker_matches_policy": bool(route) and ledger.worker == expected and ledger.calls_by_model.get(ledger.model, 0) > 0,
        "router_always_lite": ledger.calls_by_model.get(ledger.lite_model, 0) >= 1,
        "no_tool_errors": ledger.tool_errors == 0,
        "answer_nonempty": bool(answer.strip()),
        "fetched_when_url_given": not URL.search(args.request) or ledger.calls_by_tool.get("fetch", 0) >= 1,
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
                    "checks": {**checks, "route": route.tier if route else None, "difficulty": route.total if route else None},
                    "output_chars": len(answer),
                },
                indent=2,
            )
        )
    for name, ok in checks.items():
        if not ok:
            print(f"✗ check failed: {name}", file=sys.stderr)
    if not passed:
        return 1
    print(f"✓ Routed to the {route.tier} model; the worker that ran matches the routing policy and the answer is non-empty.", file=sys.stderr)
    return 0


def _assessment_from_route(route) -> dict:
    return dict(route.scores)


if __name__ == "__main__":
    raise SystemExit(main())
