"""CLI: uv run python -m security_triager /path/to/repo | --demo [-o triage.md] [--metrics m.json]"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types

from .agent import build_agent, default_ledger
from .ledger import check_report, verdict_counts

DEMO_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "demo_app"


def make_demo_repo() -> Path:
    """Copy the bundled, deliberately flawed demo app into a throwaway git repository."""
    repo = Path(tempfile.mkdtemp(prefix="triager-demo-")) / "demo_app"
    shutil.copytree(DEMO_DIR, repo)
    git = ["git", "-c", "user.name=demo", "-c", "user.email=demo@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
    subprocess.run([*git, "add", "."], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "demo app"], cwd=repo, check=True)
    return repo


async def run(repo: Path) -> tuple[str, object]:
    ledger = default_ledger(str(repo))
    runner = InMemoryRunner(agent=build_agent(ledger), app_name="security_triager")
    session = await runner.session_service.create_session(app_name="security_triager", user_id="cli")
    report = ""
    try:
        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=f"Scan {repo}")]),
        ):
            for call in event.get_function_calls():
                print(f"  → {call.name}({_short(call.args)})", file=sys.stderr)
            if event.is_final_response() and event.content and event.content.parts:
                report = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()
    return report, ledger


def _short(args: dict | None) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items() if k != "repo_path")
    return text if len(text) < 90 else text[:87] + "..."


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Scan a git repository with Semgrep and triage each finding.")
    parser.add_argument("repo", nargs="?", type=Path, help="path of a local git checkout")
    parser.add_argument("--demo", action="store_true", help="scan the bundled, deliberately flawed demo app")
    parser.add_argument("-o", "--out", type=Path, help="write the triage report to this file")
    parser.add_argument("--metrics", type=Path, help="write run metrics as JSON (used by the eval dashboard)")
    args = parser.parse_args()

    if bool(args.repo) == args.demo:
        parser.error("give a repository path or --demo (not both)")
    repo = make_demo_repo() if args.demo else args.repo.expanduser().resolve()
    if not (repo / ".git").is_dir():
        print(f"{repo} is not the root of a git checkout.", file=sys.stderr)
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        print("Set GOOGLE_API_KEY (see .env.example).", file=sys.stderr)
        return 2

    started = time.perf_counter()
    report, ledger = asyncio.run(run(repo))
    elapsed = time.perf_counter() - started

    checks = check_report(report, ledger)
    passed = checks["scan_ran"] and checks["no_invented_findings"] and checks["all_findings_triaged"]

    counts = verdict_counts(report)
    report = report.rstrip() + "\n\n---\nVerdicts (counted from the table above): " + ", ".join(f"{n} {v}" for v, n in counts.items()) + "\n"
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
                    "checks": {**checks, "verdicts": counts},
                    "output_chars": len(report),
                },
                indent=2,
            )
        )
    if not checks["scan_ran"]:
        print("✗ The agent never ran a scan.", file=sys.stderr)
    if checks["invented_ids"]:
        print(f"✗ Report cites findings the scanner never returned: {', '.join(checks['invented_ids'])}", file=sys.stderr)
    if checks["untriaged_ids"]:
        print(f"✗ Findings without a verdict: {', '.join(checks['untriaged_ids'])}", file=sys.stderr)
    if not passed:
        return 1
    print(f"✓ All {checks['findings']} scanner findings triaged; none invented.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
