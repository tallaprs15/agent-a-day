"""Per-run bookkeeping (budgets, token meter, evidence) and the checks that grade the report.

One RunLedger is created per run and shared by the agent's callbacks, so the CLI can report
cost and verify the audit once the run finishes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .rubric import BY_ID, RUBRIC, SEVERITIES, STATUSES, host_of

# Approximate list prices in USD per 1M tokens (input, output), for budgeting, not billing.
# Models not listed report no cost unless GEMINI_PRICE_IN / GEMINI_PRICE_OUT are set.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {}

URL = re.compile(r"https?://[^\s)\]>\"'`|]+")
BACKTICKED = re.compile(r"`([^`\n]+)`")
FIELD = r"^\s*[-*]\s*\*\*{name}:?\*\*:?(.*)$"
FINDING_HEADING = re.compile(r"^###\s+(A11Y-\d+)", re.M)
SCORE_ROW = re.compile(r"^\|\s*(\d\.\d\.\d)\s*\|[^|\n]*\|\s*([A-Za-z ]+?)\s*\|\s*([^|\n]*?)\s*\|?\s*$", re.M)
REQUIRED_SECTIONS = ["## Summary", "## Scorecard", "## Findings"]
MIN_QUOTE_CHARS = 4


def target_from_message(text: str) -> str | None:
    """The first http(s) URL in the user's message."""
    match = URL.search(text or "")
    return match.group(0).rstrip(".,;:") if match else None


@dataclass
class RunLedger:
    model: str
    target_url: str = ""
    invocation_id: str = ""
    max_tool_calls: int = 12
    max_pages: int = 3
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    pages: list[str] = field(default_factory=list)  # distinct URLs the agent navigated to
    evidence: list[str] = field(default_factory=list)  # what the model saw from tools
    blocked: list[str] = field(default_factory=list)  # guardrail rejections, for the run summary

    def start(self, target_url: str, invocation_id: str = "") -> None:
        """Begin a new run. `adk web` reuses one ledger for every message, so everything resets here."""
        self.target_url = target_url
        self.invocation_id = invocation_id
        self.tool_calls = 0
        self.calls_by_tool = {}
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.model_calls = 0
        self.pages = []
        self.evidence = []
        self.blocked = []

    def record_tool_call(self, name: str) -> bool:
        """Count a tool call. Returns False when the budget is exhausted."""
        if self.tool_calls >= self.max_tool_calls:
            return False
        self.tool_calls += 1
        self.calls_by_tool[name] = self.calls_by_tool.get(name, 0) + 1
        return True

    def record_page(self, url: str) -> bool:
        """Remember a navigation. Returns False when it would exceed the page budget."""
        if url in self.pages:
            return True
        if len(self.pages) >= self.max_pages:
            return False
        self.pages.append(url)
        return True

    def record_usage(self, prompt: int | None, output: int | None) -> None:
        self.model_calls += 1
        self.prompt_tokens += prompt or 0
        self.output_tokens += output or 0

    def record_evidence(self, text: str) -> None:
        self.evidence.append(text)

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _norm(url: str) -> str:
    return url.rstrip(".,;:").split("#", 1)[0].rstrip("/")


def missing_sections(report: str) -> list[str]:
    return [s for s in REQUIRED_SECTIONS if s not in report]


def _section(report: str, heading: str) -> str:
    if heading not in report:
        return ""
    return re.split(r"^## ", report.split(heading, 1)[1], maxsplit=1, flags=re.M)[0]


def scorecard(report: str) -> list[tuple[str, str, list[str]]]:
    """Rows of the Scorecard table as (criterion id, status, finding ids)."""
    rows = []
    for cid, status, refs in SCORE_ROW.findall(_section(report, "## Scorecard")):
        rows.append((cid, status.strip(), re.findall(r"A11Y-\d+", refs)))
    return rows


def findings(report: str) -> dict[str, str]:
    """finding id -> text of its `### A11Y-n` block."""
    section = _section(report, "## Findings")
    parts = FINDING_HEADING.split(section)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def _field(block: str, name: str) -> str:
    match = re.search(FIELD.format(name=name), block, re.M | re.I)
    return match.group(1).strip() if match else ""


def rubric_problems(report: str) -> list[str]:
    """Ways the report departs from the rubric. Empty means the scorecard and findings agree.

    * every rubric criterion is graded exactly once, with a valid status
    * every Fail row points at findings, and each finding names that same criterion
    * every finding is listed under a Fail row, with a valid severity
    """
    problems: list[str] = []
    rows = scorecard(report)
    graded: dict[str, str] = {}
    for cid, status, _ in rows:
        if cid not in BY_ID:
            problems.append(f"{cid}: not in the rubric")
        elif cid in graded:
            problems.append(f"{cid}: graded twice")
        elif status not in STATUSES:
            problems.append(f"{cid}: status '{status}' is not one of {', '.join(STATUSES)}")
        else:
            graded[cid] = status
    problems += [f"{c.id}: not graded" for c in RUBRIC if c.id not in graded and c.id not in {r[0] for r in rows}]

    blocks = findings(report)
    listed: set[str] = set()
    for cid, status, refs in rows:
        if graded.get(cid) != "Fail":
            continue
        if not refs:
            problems.append(f"{cid}: marked Fail but lists no finding")
        for ref in refs:
            listed.add(ref)
            if ref not in blocks:
                problems.append(f"{cid}: lists {ref}, which has no finding")
            elif _field(blocks[ref], "WCAG").strip("` ") != cid:
                problems.append(f"{ref}: names WCAG {_field(blocks[ref], 'WCAG') or '(none)'}, scorecard says {cid}")
    for fid, block in blocks.items():
        if fid not in listed:
            problems.append(f"{fid}: not listed under a Fail row in the scorecard")
        if _field(block, "Severity").strip("` ").lower() not in SEVERITIES:
            problems.append(f"{fid}: severity must be one of {', '.join(SEVERITIES)}")
    return problems


def unverified_evidence(report: str, ledger: RunLedger) -> list[str]:
    """Evidence quotes that no tool result contains, plus findings with no quote at all."""
    seen = _squash("\n".join(ledger.evidence))
    bad: list[str] = []
    for fid, block in findings(report).items():
        quotes = [q for q in BACKTICKED.findall(_field_line(block, "Evidence")) if len(q.strip()) >= MIN_QUOTE_CHARS]
        if not quotes:
            bad.append(f"{fid}: no quoted evidence")
        bad += [q for q in quotes if _squash(q.strip()) not in seen]
    return bad


def _field_line(block: str, name: str) -> str:
    return "\n".join(re.findall(FIELD.format(name=name), block, re.M | re.I))


def unverified_pages(report: str, ledger: RunLedger) -> list[str]:
    """URLs on the target's host cited in the report that no tool result mentions."""
    seen = {_norm(u) for u in URL.findall("\n".join(ledger.evidence))}
    host = host_of(ledger.target_url)
    cited = {_norm(u) for u in URL.findall(report) if host_of(u) == host}
    return sorted(cited - seen)
