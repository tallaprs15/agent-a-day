"""Per-run bookkeeping: tool-call budget, token meter and the time zones the tools were actually asked about.

One RunLedger is created per run and shared by the agent's callbacks, so the CLI can report cost and check
that the plan only uses zones the time server was consulted about.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Approximate list prices in USD per 1M tokens (input, output), for budgeting,
# not billing. For models not listed, set GEMINI_PRICE_IN / GEMINI_PRICE_OUT.
DEFAULT_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

GREETINGS = {"hi", "hello", "hey", "yo", "thanks", "thank you", "ok", "okay"}


def request_from_message(text: str) -> str | None:
    """Any real request works; a bare greeting does not."""
    text = (text or "").strip()
    if len(text) < 3 or text.lower().strip("!. ") in GREETINGS:
        return None
    return text


@dataclass
class RunLedger:
    model: str
    request: str = ""
    max_tool_calls: int = 10
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    zones_checked: set[str] = field(default_factory=set)

    def start(self, request: str) -> None:
        """Reset the per-run counters when a new user message arrives (adk web reuses one ledger)."""
        self.request = request
        self.tool_calls = self.prompt_tokens = self.output_tokens = self.model_calls = 0
        self.calls_by_tool = {}
        self.zones_checked = set()

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
