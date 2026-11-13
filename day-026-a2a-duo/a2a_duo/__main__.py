"""CLI:
  uv run python -m a2a_duo "speculative decoding" [-o brief.md] [--metrics m.json]
  uv run python -m a2a_duo serve [--port 8001]     # librarian only, for `adk web` on the writer
"""

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

from .agent import build_writer, card_url, default_ledger
from .ledger import ungrounded_librarian_ids, unverified_citations
from .server import BackgroundServer, serve_forever


async def run(topic: str):
    ledger = default_ledger()
    brief = ""
    async with BackgroundServer(ledger) as server:
        print(f"  librarian serving A2A at {card_url(server.port)}", file=sys.stderr)
        runner = InMemoryRunner(agent=build_writer(ledger, card_url(server.port)), app_name="a2a_duo")
        session = await runner.session_service.create_session(app_name="a2a_duo", user_id="cli")
        try:
            async for event in runner.run_async(
                user_id="cli",
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text=f"Topic: {topic}")]),
            ):
                for call in event.get_function_calls():
                    print(f"  → {call.name}({_short(call.args)})", file=sys.stderr)
                if event.is_final_response() and event.content and event.content.parts:
                    brief = "".join(p.text or "" for p in event.content.parts)
        finally:
            await runner.close()
    return brief, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    # The A2A hop is localhost: keep it off any HTTP proxy.
    os.environ["NO_PROXY"] = ",".join(filter(None, [os.getenv("NO_PROXY"), "127.0.0.1,localhost"]))

    parser = argparse.ArgumentParser(description="Two agents over A2A: a librarian finds papers, a writer cites them.")
    parser.add_argument("topic", help='research topic, or "serve" to run the librarian server only')
    parser.add_argument("-o", "--out", type=Path, help="write the brief to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    parser.add_argument("--port", type=int, default=8001, help="port for `serve`")
    args = parser.parse_args()

    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_USE_VERTEXAI")):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2
    if args.topic == "serve":
        serve_forever(args.port)
        return 0

    started = time.perf_counter()
    brief, ledger = asyncio.run(run(args.topic))
    elapsed = time.perf_counter() - started

    if args.out:
        args.out.write_text(brief)
    else:
        print(brief)

    bad_cites = unverified_citations(brief, ledger)
    bad_ids = ungrounded_librarian_ids(ledger)
    delegated = ledger.delegations > 0
    checks = {
        "delegated_over_a2a": delegated,
        "librarian_grounded": not bad_ids,
        "citations_verified": not bad_cites and bool(brief),
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
                    "checks": {**checks, "unverified_ids": bad_cites, "ungrounded_librarian_ids": bad_ids},
                    "output_chars": len(brief),
                },
                indent=2,
            )
        )
    if not delegated:
        print("✗ The writer never called the librarian over A2A.", file=sys.stderr)
    if bad_ids:
        print(f"✗ Librarian reported IDs no arXiv tool returned: {', '.join(bad_ids)}", file=sys.stderr)
    if bad_cites:
        print(f"✗ Brief cites IDs the librarian never sent: {', '.join(bad_cites)}", file=sys.stderr)
    if passed:
        print("✓ Delegated over A2A; every cited ID traces back to an arXiv tool result.", file=sys.stderr)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
