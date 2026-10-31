"""Per-run bookkeeping and the deterministic checks run after the debate.

One RunLedger is shared by the agents' callbacks: it enforces the tool-call budget, meters tokens, and
remembers every arXiv ID a tool really returned, so the transcript and the ruling can be checked without
asking a model.
"""

from __future__ import annotations

import json
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
ARXIV_ID = re.compile(r"(?<![\w.])(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?(?![\w])")
CLAIM_LINE = re.compile(r"^\s*(?:[-*]\s*)?\**(C\d+)\**\s*[:.)]", re.M)
MIN_CLAIMS, MAX_CLAIMS = 2, 5


def normalize(arxiv_id: str) -> str:
    """Strip a version suffix so 2401.12345v2 matches 2401.12345."""
    return re.sub(r"v\d+$", "", arxiv_id)


def find_arxiv_ids(text: str) -> list[str]:
    return [normalize(i) for i in ARXIV_ID.findall(text)]


def claim_ids(brief: str) -> list[str]:
    """Claim labels (C1, C2, ...) in the order the reader listed them."""
    seen: list[str] = []
    for label in CLAIM_LINE.findall(brief):
        if label not in seen:
            seen.append(label)
    return seen


@dataclass
class RunLedger:
    model: str
    max_tool_calls: int = 10
    tool_calls: int = 0
    rounds: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    seen_arxiv_ids: set[str] = field(default_factory=set)

    def start_round(self) -> None:
        self.rounds += 1

    def record_tool_call(self, name: str) -> str | None:
        """Count a tool call. Returns None if allowed, else the reason it was refused."""
        if self.tool_calls >= self.max_tool_calls:
            return f"Tool budget of {self.max_tool_calls} calls reached. Make your argument with what you have."
        self.tool_calls += 1
        self.calls_by_tool[name] = self.calls_by_tool.get(name, 0) + 1
        return None

    def record_usage(self, prompt: int | None, output: int | None) -> None:
        self.model_calls += 1
        self.prompt_tokens += prompt or 0
        self.output_tokens += output or 0

    def record_evidence(self, text: str) -> None:
        """Remember every arXiv ID that came back from a tool."""
        self.seen_arxiv_ids.update(find_arxiv_ids(text))

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000


def unverified_citations(text: str, ledger: RunLedger) -> list[str]:
    """arXiv IDs cited in the text that no tool ever returned (likely hallucinated)."""
    return sorted(set(find_arxiv_ids(text)) - ledger.seen_arxiv_ids)


def as_ruling(state: dict) -> dict:
    """The judge's ruling from session state; ADK stores it as a dict, or as JSON text on some paths."""
    ruling = state.get("ruling") or {}
    if isinstance(ruling, str):
        try:
            ruling = json.loads(ruling)
        except ValueError:
            ruling = {}
    return ruling if isinstance(ruling, dict) else {}


def debate_checks(state: dict, ledger: RunLedger, rounds_expected: int) -> dict[str, bool]:
    """Deterministic post-run checks. All must be True for the run to pass."""
    brief = state.get("brief") or ""
    transcript = state.get("transcript") or ""
    ruling = as_ruling(state)
    labels = claim_ids(brief)
    ruled = [c.get("claim_id") for c in ruling.get("claims", [])]
    report_text = transcript + " " + str(ruling)
    return {
        "claims_extracted": MIN_CLAIMS <= len(labels) <= MAX_CLAIMS,
        "both_sides_heard": all(
            transcript.count(f"Round {n} · {side}") == 1 for n in range(1, rounds_expected + 1) for side in ("Advocate", "Skeptic")
        ),
        "ruling_covers_every_claim": bool(labels) and sorted(ruled) == sorted(labels),
        "citations_verified": not unverified_citations(report_text, ledger),
    }
