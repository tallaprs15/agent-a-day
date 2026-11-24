"""Per-run bookkeeping: tool-call budget, token meter, evidence trail and output checks.

One RunLedger is created per run and shared by the agent's callbacks, so the
CLI can report cost and verify the answer's doc links once the run finishes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

# Approximate list prices in USD per 1M tokens (input, output), for budgeting,
# not billing. For models not listed, set GEMINI_PRICE_IN / GEMINI_PRICE_OUT.
DEFAULT_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

DOCS_HOST = "developers.cloudflare.com"
REQUIRED_SECTIONS = ["## Answer", "## Sources"]
# Markdown links or bare URLs that point at the Cloudflare docs.
DOC_LINK = re.compile(r"https://developers\.cloudflare\.com(/[^\s)\]<>\"'`]*)?")
# Root-relative links inside doc pages, e.g. [limits](/kv/platform/limits/).
RELATIVE_LINK = re.compile(r"\]\((/[^)\s]*)\)")


def doc_path(url: str) -> str:
    """Canonical form of a docs link: path only, no fragment/query, no trailing slash."""
    path = urlparse(url.strip().rstrip(".,;:")).path or "/"
    return path.rstrip("/") or "/"


@dataclass
class RunLedger:
    model: str
    max_tool_calls: int = 6
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    seen_paths: set[str] = field(default_factory=set)

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
        """Remember every docs page the server returned, plus the pages those pages link to."""
        self.seen_paths.update(doc_path(m.group(0)) for m in DOC_LINK.finditer(text))
        self.seen_paths.update(doc_path(m.group(1)) for m in RELATIVE_LINK.finditer(text))

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000


def cited_links(answer: str) -> list[str]:
    """Every Cloudflare docs link in the answer, de-duplicated, in order."""
    seen: dict[str, None] = {}
    for m in DOC_LINK.finditer(answer):
        seen[m.group(0).rstrip(".,;:")] = None
    return list(seen)


def unverified_links(answer: str, ledger: RunLedger) -> list[str]:
    """Docs links in the answer that the docs server never returned (likely invented)."""
    return [u for u in cited_links(answer) if doc_path(u) not in ledger.seen_paths]


def missing_sections(answer: str) -> list[str]:
    return [s for s in REQUIRED_SECTIONS if s not in answer]
