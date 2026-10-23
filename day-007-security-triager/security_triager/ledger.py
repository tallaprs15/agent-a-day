"""Per-run bookkeeping: tool-call budget, token meter, the files the agent read and the findings Semgrep returned.

One RunLedger is created per run and shared by the agent's callbacks, so the CLI can report cost and check that
the triage report covers exactly the findings the scanner produced.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

# Approximate list prices in USD per 1M tokens (input, output), for budgeting,
# not billing. For models not listed, set GEMINI_PRICE_IN / GEMINI_PRICE_OUT.
DEFAULT_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

VERDICTS = ("exploitable", "likely", "unlikely", "false positive")
FINDING_ID = re.compile(r"\bF\d+\b")
MAX_FILE_CHARS = 12_000  # per file, so one large source file can't dominate the context
GREETINGS = {"hi", "hello", "hey", "yo", "thanks", "thank you", "ok", "okay"}


def repo_from_message(text: str) -> str | None:
    """The repository path is the first path-looking token in the message ('Repo: /tmp/x', 'scan ~/code/app')."""
    text = (text or "").strip()
    if len(text) < 2 or text.lower().strip("!. ") in GREETINGS:
        return None
    for token in text.replace("`", " ").split():
        token = token.strip("\"'.,;:()")
        if token.startswith(("/", "~", "./", "../")):
            return os.path.abspath(os.path.expanduser(token))
    return None


def extract_text(tool_response: Any) -> str:
    """Flatten an MCP tool response (dict with content parts, structured content or plain text) to text."""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        parts = tool_response.get("content")
        if isinstance(parts, list):
            return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if "result" in tool_response:
            return extract_text(tool_response["result"])
    return json.dumps(tool_response, default=str)


@dataclass
class RunLedger:
    model: str
    repo: str = ""
    max_tool_calls: int = 14
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    scans: int = 0
    files: dict[str, str] = field(default_factory=dict)  # repo-relative path -> content the model has seen
    findings: list[dict] = field(default_factory=list)

    def start(self, repo: str) -> None:
        """Reset the per-run state when a new repository is requested (adk web reuses one ledger)."""
        self.repo = repo
        self.tool_calls = self.prompt_tokens = self.output_tokens = self.model_calls = self.scans = 0
        self.calls_by_tool = {}
        self.files = {}
        self.findings = []

    def record_tool_call(self, name: str) -> bool:
        """Count a tool call. Returns False when the budget is exhausted."""
        if self.tool_calls >= self.max_tool_calls:
            return False
        self.tool_calls += 1
        self.calls_by_tool[name] = self.calls_by_tool.get(name, 0) + 1
        return True

    def record_usage(self, prompt: int | None, output: int | None) -> None:
        self.model_calls += 1
        self.prompt_tokens += prompt or 0
        self.output_tokens += output or 0

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000

    def add_scan(self, scan_json: str) -> list[dict]:
        """Turn Semgrep's JSON into compact findings with stable ids (F1, F2, ...). Returns the new findings."""
        self.scans += 1
        known = {(f["rule"], f["path"], f["line"]) for f in self.findings}
        added = []
        for hit in json.loads(scan_json).get("results", []):
            key = (hit["check_id"], hit["path"], hit["start"]["line"])
            if key in known:
                continue
            known.add(key)
            finding = {
                "id": f"F{len(self.findings) + 1}",
                "rule": key[0],
                "path": key[1],
                "line": key[2],
                "severity": hit["extra"].get("severity", ""),
                "message": hit["extra"].get("message", ""),
                "code": self.excerpt(key[1], key[2]),
            }
            self.findings.append(finding)
            added.append(finding)
        return added

    def excerpt(self, path: str, line: int, context: int = 2) -> str:
        lines = self.files.get(path, "").splitlines()
        lo, hi = max(0, line - 1 - context), min(len(lines), line + context)
        return "\n".join(f"{n + 1}: {lines[n]}" for n in range(lo, hi))


def check_report(report: str, ledger: RunLedger) -> dict[str, Any]:
    """Deterministic checks on the triage: every scanner finding is covered once, with a verdict, and none is invented."""
    known = {f["id"] for f in ledger.findings}
    rows = [ln for ln in report.splitlines() if ln.lstrip().startswith("|") and FINDING_ID.search(ln)]
    cited = {i for ln in rows for i in FINDING_ID.findall(ln.split("|")[1] if ln.count("|") > 1 else ln)}
    with_verdict = {
        i
        for ln in rows
        for i in FINDING_ID.findall(ln.split("|")[1] if ln.count("|") > 1 else ln)
        if any(v in ln.lower() for v in VERDICTS)
    }
    invented = sorted(cited - known)
    missing = sorted(known - with_verdict)
    return {
        "scan_ran": ledger.scans > 0,
        "no_invented_findings": not invented,
        "all_findings_triaged": not missing,
        "invented_ids": invented,
        "untriaged_ids": missing,
        "findings": len(known),
    }


def verdict_counts(report: str) -> dict[str, int]:
    """Count verdicts from the findings table itself, so the totals never depend on the model's arithmetic."""
    counts = dict.fromkeys(VERDICTS, 0)
    for line in report.splitlines():
        cells = [c.strip().lower() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and FINDING_ID.fullmatch(cells[0].upper()) and cells[3] in counts:
            counts[cells[3]] += 1
    return counts
