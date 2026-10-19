"""Run every agent's eval task live and append the results to the dashboard data.

Each day-*/meta.json may define:  "eval": {"args": ["<cli args>"], "timeout_s": 300}
The runner calls the agent's own CLI with `--metrics` and records pass/fail,
the metrics JSON, and the tail of stderr.

    python3 scripts/run_evals.py --site site        # all agents
    python3 scripts/run_evals.py --site site --only day-001
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEEP_RUNS = 120


def package_of(day_dir: Path) -> str | None:
    for main in sorted(day_dir.glob("*/__main__.py")):
        if (main.parent / "agent.py").exists():
            return main.parent.name
    return None


def scrub(text: str) -> str:
    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        value = os.getenv(name)
        if value:
            text = text.replace(value, "[redacted]")
    return text


def run_one(day_dir: Path) -> dict:
    meta = json.loads((day_dir / "meta.json").read_text())
    base = {
        "day": meta["day"],
        "slug": meta["slug"],
        "title": meta["title"],
        "pattern": meta.get("pattern", ""),
        "folder": day_dir.name,
        "model": meta.get("model"),
    }
    spec = meta.get("eval")
    pkg = package_of(day_dir)
    if not spec or not pkg:
        return {**base, "status": "no-eval"}

    subprocess.run(["uv", "sync", "-q"], cwd=day_dir, check=False, capture_output=True)
    with tempfile.TemporaryDirectory() as tmp:
        out, metrics_path = Path(tmp) / "out.md", Path(tmp) / "metrics.json"
        cmd = ["uv", "run", "-q", "python", "-m", pkg, *spec["args"], "-o", str(out), "--metrics", str(metrics_path)]
        try:
            proc = subprocess.run(cmd, cwd=day_dir, capture_output=True, text=True, timeout=spec.get("timeout_s", 300))
        except subprocess.TimeoutExpired as exc:
            tail = scrub((exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
            return {**base, "status": "timeout", "stderr_tail": "\n".join(tail.splitlines()[-20:])}

        metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else None
        if metrics is None:
            status = "error"
        elif proc.returncode == 0 and metrics.get("passed", True):
            status = "pass"
        else:
            status = "fail"
        return {
            **base,
            "status": status,
            "exit_code": proc.returncode,
            "metrics": metrics,
            "stderr_tail": "\n".join(scrub(proc.stderr).splitlines()[-20:]),
            "output_excerpt": out.read_text()[:800] if out.exists() else "",
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", type=Path, required=True, help="gh-pages checkout to write into")
    parser.add_argument("--only", help="run just the day folders starting with this prefix")
    args = parser.parse_args()

    days = sorted(p for p in ROOT.glob("day-*") if (p / "meta.json").exists())
    if args.only:
        days = [d for d in days if d.name.startswith(args.only)]

    results = []
    for day_dir in days:
        print(f"→ {day_dir.name}", flush=True)
        result = run_one(day_dir)
        print(f"  {result['status']}", flush=True)
        results.append(result)

    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    run = {"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "sha": sha, "results": results}

    data = args.site / "data"
    data.mkdir(parents=True, exist_ok=True)
    history_path = data / "history.json"
    history = json.loads(history_path.read_text()) if history_path.exists() else []
    history = (history + [run])[-KEEP_RUNS:]
    history_path.write_text(json.dumps(history, indent=1))
    (data / "latest.json").write_text(json.dumps(run, indent=1))

    evaluated = [r for r in results if r["status"] != "no-eval"]
    passing = sum(r["status"] == "pass" for r in evaluated)
    color = "brightgreen" if evaluated and passing == len(evaluated) else ("orange" if passing else "red")
    (args.site / "badge.json").write_text(
        json.dumps({"schemaVersion": 1, "label": "agents passing", "message": f"{passing}/{len(evaluated)}", "color": color})
    )
    shutil.copy(ROOT / "dashboard" / "index.html", args.site / "index.html")
    (args.site / ".nojekyll").touch()
    print(f"agents passing: {passing}/{len(evaluated)}")


if __name__ == "__main__":
    main()
