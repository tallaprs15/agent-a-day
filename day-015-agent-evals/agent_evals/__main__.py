"""CLI: uv run python -m agent_evals [-o report.md] [--metrics m.json] [--runs N]

Runs the Time Desk eval set (trajectory + response scoring) and exits non-zero if a threshold is missed.
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

from .cases import CASES, CRITERIA


def render_report(scores, checks: dict[str, bool]) -> str:
    metrics = list(CRITERIA)
    lines = ["# Time Desk eval report", "", "| Case | " + " | ".join(metrics) + " |", "|---|" + "---|" * len(metrics)]
    for eval_id, *_ in CASES:
        row = scores.per_case.get(eval_id, {})
        lines.append(f"| {eval_id} | " + " | ".join(f"{row.get(m, 0.0):.2f}" for m in metrics) + " |")
    lines += ["", "| Metric | Mean | Threshold | Result |", "|---|---|---|---|"]
    for m, t in CRITERIA.items():
        lines.append(f"| {m} | {scores.per_metric.get(m, 0.0):.2f} | {t} | {'pass' if checks[m] else 'FAIL'} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Run the Time Desk eval set.")
    parser.add_argument("-o", "--out", type=Path, help="write the report to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    parser.add_argument("--runs", type=int, default=1, help="times to run each case (default 1)")
    args = parser.parse_args()

    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_USE_VERTEXAI")):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    from .agent import LEDGER
    from .suite import run_suite

    started = time.perf_counter()
    scores = asyncio.run(run_suite(args.runs))
    elapsed = time.perf_counter() - started
    checks = scores.checks(CRITERIA)
    report = render_report(scores, checks)
    if args.out:
        args.out.write_text(report)
    else:
        print(report)

    cost = LEDGER.cost_usd()
    print(
        f"\n— {elapsed:.1f}s · {LEDGER.model_calls} model calls · {LEDGER.tool_calls}/{LEDGER.max_tool_calls} tool calls "
        f"{LEDGER.calls_by_tool} · {LEDGER.prompt_tokens:,} in / {LEDGER.output_tokens:,} out tokens"
        + (f" · ~${cost:.4f}" if cost is not None else ""),
        file=sys.stderr,
    )
    passed = all(checks.values())
    if args.metrics:
        args.metrics.write_text(json.dumps({
            "passed": passed,
            "elapsed_s": round(elapsed, 2),
            "model": LEDGER.model,
            "model_calls": LEDGER.model_calls,
            "tool_calls": LEDGER.tool_calls,
            "tool_budget": LEDGER.max_tool_calls,
            "calls_by_tool": LEDGER.calls_by_tool,
            "prompt_tokens": LEDGER.prompt_tokens,
            "output_tokens": LEDGER.output_tokens,
            "cost_usd": cost,
            "checks": {**checks, "scores": {m: round(v, 3) for m, v in scores.per_metric.items()}},
            "output_chars": len(report),
        }, indent=2))
    for m, ok in checks.items():
        print(f"{'✓' if ok else '✗'} {m} = {scores.per_metric.get(m, 0.0):.2f} (threshold {CRITERIA[m]})", file=sys.stderr)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
