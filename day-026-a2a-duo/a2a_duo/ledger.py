"""Per-run bookkeeping shared by both agents: budgets, token meter and evidence trail.

Both agents run in one process for the CLI, so one RunLedger sees everything. Over a real
network each side would keep its own; the checks below only need the librarian's reply text
(client side) and the MCP tool results (server side).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Approximate list prices in USD per 1M tokens (input, output), for budgeting, not billing.
# For models not listed, set GEMINI_PRICE_IN / GEMINI_PRICE_OUT.
DEFAULT_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

# New-style (2401.12345, optional vN) and old-style (cs/0101001) arXiv IDs.
ARXIV_ID = re.compile(r"\b(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?\b")


def normalize(arxiv_id: str) -> str:
    """Strip a version suffix so 2401.12345v2 matches 2401.12345."""
    return re.sub(r"v\d+$", "", arxiv_id)


def ids_in(text: str) -> set[str]:
    return {normalize(i) for i in ARXIV_ID.findall(text)}


@dataclass
class RunLedger:
    model: str
    max_tool_calls: int = 8  # MCP calls made by the librarian
    max_delegations: int = 2  # A2A requests the writer may send to the librarian
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    delegations: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    mcp_ids: set[str] = field(default_factory=set)  # IDs returned by arXiv tools (librarian side)
    librarian_ids: set[str] = field(default_factory=set)  # IDs in the librarian's A2A replies (writer side)

    def record_tool_call(self, name: str) -> bool:
        """Count an MCP tool call. Returns False when the budget is exhausted."""
        if self.tool_calls >= self.max_tool_calls:
            return False
        self.tool_calls += 1
        self.calls_by_tool[name] = self.calls_by_tool.get(name, 0) + 1
        return True

    def record_delegation(self) -> bool:
        """Count an A2A request to the librarian. Returns False when the cap is reached."""
        if self.delegations >= self.max_delegations:
            return False
        self.delegations += 1
        self.calls_by_tool["a2a:librarian"] = self.calls_by_tool.get("a2a:librarian", 0) + 1
        return True

    def record_usage(self, prompt: int | None, output: int | None) -> None:
        self.model_calls += 1
        self.prompt_tokens += prompt or 0
        self.output_tokens += output or 0

    def record_mcp_evidence(self, text: str) -> None:
        self.mcp_ids |= ids_in(text)

    def record_librarian_reply(self, text: str) -> None:
        self.librarian_ids |= ids_in(text)

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000


def ungrounded_librarian_ids(ledger: RunLedger) -> list[str]:
    """IDs the librarian reported over A2A that no arXiv tool call ever returned."""
    return sorted(ledger.librarian_ids - ledger.mcp_ids)


def unverified_citations(brief: str, ledger: RunLedger) -> list[str]:
    """IDs cited in the brief that the librarian never sent over A2A."""
    return sorted(ids_in(brief) - ledger.librarian_ids)
