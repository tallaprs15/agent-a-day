"""Per-run bookkeeping: tool-call budget, token meter, and evidence trail.

One RunLedger is created per run and shared by the agent's callbacks, so the
CLI can report cost and verify citations once the run finishes.
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

# New-style (2401.12345, optional vN) and old-style (cs/0101001) arXiv IDs.
ARXIV_ID = re.compile(r"\b(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?\b")


@dataclass
class RunLedger:
    model: str
    max_tool_calls: int = 12
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    seen_arxiv_ids: set[str] = field(default_factory=set)

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
        """Remember every arXiv ID that came back from a tool."""
        self.seen_arxiv_ids.update(normalize(i) for i in ARXIV_ID.findall(text))

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000


def normalize(arxiv_id: str) -> str:
    """Strip a version suffix so 2401.12345v2 matches 2401.12345."""
    return re.sub(r"v\d+$", "", arxiv_id)


def unverified_citations(brief: str, ledger: RunLedger) -> list[str]:
    """arXiv IDs cited in the brief that no tool ever returned (likely hallucinated)."""
    cited = {normalize(i) for i in ARXIV_ID.findall(brief)}
    return sorted(cited - ledger.seen_arxiv_ids)
