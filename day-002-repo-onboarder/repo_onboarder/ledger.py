"""Per-run bookkeeping: tool-call budget, token meter, evidence trail and output checks.

One RunLedger is created per run and shared by every stage's callbacks, so the
CLI can report cost and verify the guide once the pipeline finishes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Approximate list prices in USD per 1M tokens (input, output), for budgeting,
# not billing. For models not listed, set GEMINI_PRICE_IN / GEMINI_PRICE_OUT.
DEFAULT_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

REPO_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
URL = re.compile(r"https?://[^\s)\]>\"'`]+")
REQUIRED_SECTIONS = [
    "## What this project is",
    "## Architecture map",
    "## Where to start reading",
    "## Local setup",
    "## How to contribute",
    "## Gotchas",
]


def parse_repo(value: str) -> str:
    """Accept 'owner/name' or a github.com URL; return 'owner/name'."""
    value = value.strip().removesuffix(".git").rstrip("/")
    value = re.sub(r"^(https?://)?(www\.)?github\.com/", "", value)
    if not REPO_SLUG.match(value):
        raise ValueError(f"expected 'owner/name' or a github.com URL, got {value!r}")
    return value


_GITHUB_URL = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", re.I)
_SLUG_IN_TEXT = re.compile(r"(?<![\w./-])([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+)(?![\w/])")


def extract_repo(text: str) -> str | None:
    """Find the target repo in a free-text message: a github.com URL, else the first owner/name."""
    text = text or ""
    candidates = [m.group(1) for m in _GITHUB_URL.finditer(text)] + [m.group(1) for m in _SLUG_IN_TEXT.finditer(text)]
    for candidate in candidates:
        if not re.search(r"[A-Za-z]", candidate):  # "3/4" is a fraction, not a repo
            continue
        try:
            return parse_repo(candidate)
        except ValueError:
            continue
    return None


@dataclass
class RunLedger:
    model: str
    repo: str = ""
    max_tool_calls: int = 10
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    evidence: list[str] = field(default_factory=list)

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

    def record_evidence(self, text: str) -> None:
        self.evidence.append(text)

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000


def _norm(url: str) -> str:
    return url.rstrip(".,;:").rstrip("/")


def unverified_links(guide: str, ledger: RunLedger) -> list[str]:
    """Repo file links in the guide whose path never appeared in a tool result.

    Only links into the target repo are checked: those are the ones a model tends to invent
    (plausible-looking file paths). Links to other sites are not judged here.
    """
    seen = "\n".join(ledger.evidence)
    pattern = re.compile(rf"https://github\.com/{re.escape(ledger.repo)}/(?:blob|tree)/[^/\s)]+/([^\s)\]>\"'`]+)", re.I)
    bad = []
    for match in pattern.finditer(guide):
        path = match.group(1).rstrip(".,;:/")
        if path not in seen:
            bad.append(match.group(0).rstrip(".,;:/"))
    return sorted(set(bad))


def missing_sections(guide: str) -> list[str]:
    return [s for s in REQUIRED_SECTIONS if s not in guide]
