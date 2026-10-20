"""CLI: uv run python -m web_qa_explorer https://demo.playwright.dev/todomvc/ [--focus "..."] [--risky ask|approve|deny] [-o report.md] [--metrics m.json]"""

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
from .ledger import append_approval_log, missing_sections, unverified_evidence, unverified_pages
from .risk import in_scope

CONFIRMATION_CALL = "adk_request_confirmation"  # ADK's name for a "please approve this tool call" request


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def _decide(policy: str, original: dict) -> bool:
    """Approve or reject one risky action, per the --risky policy."""
    action = f"{original.get('name')}({_short(original.get('args'))})"
    if policy == "approve":
        print(f"  ✓ approved: {action}", file=sys.stderr)
        return True
    if policy == "ask":
        answer = input(f"  ? Approve {action}? [y/N] ").strip().lower()
        return answer in ("y", "yes")
    print(f"  ✗ declined (no approval given): {action}", file=sys.stderr)
    return False


async def run(url: str, focus: str, policy: str) -> tuple[str, object]:
    ledger = default_ledger()
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="web_qa_explorer")
    session = await runner.session_service.create_session(app_name="web_qa_explorer", user_id="cli")
    message = types.Content(role="user", parts=[types.Part(text=f"{url} {focus}".strip())])
    report = ""
    try:
        while message is not None:
            decisions: list[types.Part] = []
            async for event in runner.run_async(user_id="cli", session_id=session.id, new_message=message):
                for call in event.get_function_calls():
                    if call.name == CONFIRMATION_CALL:
                        # The agent paused on a risky action: collect the human's decision, then resume.
                        original = (call.args or {}).get("originalFunctionCall") or {}
                        decisions.append(
                            types.Part(function_response=types.FunctionResponse(
                                id=call.id, name=CONFIRMATION_CALL, response={"confirmed": _decide(policy, original)}))
                        )
                    else:
                        print(f"  → {call.name}({_short(call.args)})", file=sys.stderr)
                if event.is_final_response() and event.content and event.content.parts and not event.get_function_calls():
                    report = "".join(p.text or "" for p in event.content.parts)
            message = types.Content(role="user", parts=decisions) if decisions else None
    finally:
        await runner.close()
    return report, ledger


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Explore a web app in a real browser and file an evidence-backed bug report.")
    parser.add_argument("url", help="http(s) URL of the app to test")
    parser.add_argument("--focus", default="", help="what to look at, e.g. 'add two todos and complete one'")
    parser.add_argument(
        "--risky",
        choices=["ask", "approve", "deny"],
        default="ask" if sys.stdin.isatty() else "deny",
        help="what to do when the agent wants a state-changing action (default: ask on a terminal, deny otherwise)",
    )
    parser.add_argument("-o", "--out", type=Path, help="write the report to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if not in_scope(args.url, args.url):
        print("Give a full http(s) URL, e.g. https://demo.playwright.dev/todomvc/", file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    report, ledger = asyncio.run(run(args.url, args.focus, args.risky))
    elapsed = time.perf_counter() - started
    report = append_approval_log(report, ledger)

    if args.out:
        args.out.write_text(report)
    else:
        print(report)

    cost = ledger.cost_usd()
    print(
        f"\n— {elapsed:.1f}s · {ledger.model_calls} model calls · {ledger.tool_calls}/{ledger.max_tool_calls} tool calls "
        f"{ledger.calls_by_tool} · {ledger.prompt_tokens:,} in / {ledger.output_tokens:,} out tokens"
        + (f" · ~${cost:.4f}" if cost is not None else ""),
        file=sys.stderr,
    )
    missing = missing_sections(report)
    bad_evidence = unverified_evidence(report, ledger)
    bad_pages = unverified_pages(report, ledger)
    gate_breaches = list(ledger.unapproved_risky)
    passed = not (missing or bad_evidence or bad_pages or gate_breaches)
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
                        "sections_present": not missing,
                        "evidence_verified": not bad_evidence,
                        "pages_verified": not bad_pages,
                        "risky_actions_gated": not gate_breaches,
                        "missing_sections": missing,
                        "unverified_evidence": bad_evidence,
                        "unverified_pages": bad_pages,
                        "approvals": ledger.approvals,
                        "blocked": ledger.blocked,
                    },
                    "output_chars": len(report),
                },
                indent=2,
            )
        )
    if missing:
        print(f"✗ Report is missing sections: {', '.join(missing)}", file=sys.stderr)
    if bad_evidence:
        print(f"✗ Evidence not found in any tool result: {'; '.join(bad_evidence)}", file=sys.stderr)
    if bad_pages:
        print(f"✗ Pages cited but never returned by a tool: {', '.join(bad_pages)}", file=sys.stderr)
    if gate_breaches:
        print(f"✗ Risky actions ran without approval: {', '.join(gate_breaches)}", file=sys.stderr)
    if not passed:
        return 1
    print("✓ Sections present, every quote came from a tool result, and no risky action ran unapproved.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
