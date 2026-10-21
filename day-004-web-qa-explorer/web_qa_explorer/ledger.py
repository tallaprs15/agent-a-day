"""Per-run bookkeeping: budgets, token meter, evidence trail, approval log and report checks.

One RunLedger is created per run and shared by the agent's callbacks, so the CLI can
report cost and verify the bug report once the exploration finishes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .risk import host_of

# Approximate list prices in USD per 1M tokens (input, output), for budgeting,
# not billing. For models not listed, set GEMINI_PRICE_IN / GEMINI_PRICE_OUT.
DEFAULT_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

URL = re.compile(r"https?://[^\s)\]>\"'`|]+")
BACKTICKED = re.compile(r"`([^`\n]+)`")
EVIDENCE_LINE = re.compile(r"^\s*[-*]\s*\*\*Evidence:?\*\*:?(.*)$", re.M)
FINDING_HEADING = re.compile(r"^###\s+BUG-\d+", re.M)
REQUIRED_SECTIONS = ["## Summary", "## Findings", "## Coverage"]
MIN_QUOTE_CHARS = 4


def target_from_message(text: str) -> tuple[str, str] | None:
    """(url, focus) from the user's message: the first http(s) URL, and whatever else they said."""
    match = URL.search(text or "")
    if not match:
        return None
    url = match.group(0).rstrip(".,;:")
    focus = (text[: match.start()] + " " + text[match.end():]).strip(" \n\t-:,.")
    return url, focus


@dataclass
class RunLedger:
    model: str
    target_url: str = ""
    focus: str = ""
    invocation_id: str = ""
    max_tool_calls: int = 14
    max_pages: int = 5
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    pages: list[str] = field(default_factory=list)  # distinct URLs the agent navigated to
    evidence: list[str] = field(default_factory=list)  # what the model saw from tools
    approvals: list[dict] = field(default_factory=list)  # {"action", "decision"} per risky request
    unapproved_risky: list[str] = field(default_factory=list)  # risky actions that ran without approval
    blocked: list[str] = field(default_factory=list)  # guardrail rejections, for the run summary

    def start(self, target_url: str, focus: str = "", invocation_id: str = "") -> None:
        """Begin a new run. `adk web` reuses one ledger for every message, so everything resets here."""
        self.target_url = target_url
        self.focus = focus
        self.invocation_id = invocation_id
        self.tool_calls = 0
        self.calls_by_tool = {}
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.model_calls = 0
        self.pages = []
        self.evidence = []
        self.approvals = []
        self.unapproved_risky = []
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

    def record_approval(self, action: str, approved: bool) -> None:
        self.approvals.append({"action": action, "decision": "approved" if approved else "declined"})

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
    """Compare URLs without trailing punctuation or #fragment."""
    return url.rstrip(".,;:").split("#", 1)[0].rstrip("/")


def missing_sections(report: str) -> list[str]:
    return [s for s in REQUIRED_SECTIONS if s not in report]


def findings(report: str) -> list[str]:
    """The text of each `### BUG-n` block in the report."""
    if "## Findings" not in report:
        return []
    section = re.split(r"^## ", report.split("## Findings", 1)[1], maxsplit=1, flags=re.M)[0]
    return FINDING_HEADING.split(section)[1:]


def unverified_evidence(report: str, ledger: RunLedger) -> list[str]:
    """Evidence quotes in the report that no tool result contains, plus findings with no quote at all.

    Every finding must cite at least one backticked quote (a console message, a failed request,
    a snippet of the page) and each quote must appear verbatim in what the tools returned.
    """
    seen = _squash("\n".join(ledger.evidence))
    bad: list[str] = []
    for n, block in enumerate(findings(report), start=1):
        quotes = [q for line in EVIDENCE_LINE.findall(block) for q in BACKTICKED.findall(line)]
        quotes = [q for q in quotes if len(q.strip()) >= MIN_QUOTE_CHARS]
        if not quotes:
            bad.append(f"BUG-{n}: no quoted evidence")
        bad += [q for q in quotes if _squash(q.strip()) not in seen]
    return bad


def unverified_pages(report: str, ledger: RunLedger) -> list[str]:
    """URLs on the target's host cited in the report that no tool result mentions.

    Links to other hosts (for example a spec or a docs page) are not claims about the site.
    """
    seen = {_norm(u) for u in URL.findall("\n".join(ledger.evidence))}
    host = host_of(ledger.target_url)
    cited = {_norm(u) for u in URL.findall(report) if host_of(u) == host}
    return sorted(cited - seen)


def append_approval_log(report: str, ledger: RunLedger) -> str:
    """A deterministic record of every approval request, written by code rather than by the model."""
    if not ledger.approvals:
        return report
    lines = [f"- {a['decision']}: `{a['action']}`" for a in ledger.approvals]
    return report.rstrip() + "\n\n## Approval log\n" + "\n".join(lines) + "\n"
