"""Per-run bookkeeping: tool-call budget, token meter, and the SQL the database actually executed.

One RunLedger is created per run and shared by the agent's callbacks, so the CLI can report cost and verify
that every SQL statement shown in the answer was really run.
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

GREETINGS = {"hi", "hello", "hey", "yo", "thanks", "thank you", "ok", "okay"}
SQL_BLOCK = re.compile(r"```sql\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def question_from_message(text: str) -> str | None:
    """Any real question works; a bare greeting does not."""
    text = (text or "").strip()
    if len(text) < 3 or text.lower().strip("!. ") in GREETINGS:
        return None
    return text


def normalize_sql(sql: str) -> str:
    """Whitespace-, case- and semicolon-insensitive form used to compare SQL texts."""
    return re.sub(r"\s+", " ", sql).strip().rstrip(";").strip().lower()


def sql_blocks(answer: str) -> list[str]:
    return [b.strip() for b in SQL_BLOCK.findall(answer or "") if b.strip()]


@dataclass
class RunLedger:
    model: str
    question: str = ""
    max_tool_calls: int = 12
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    executed_sql: list[str] = field(default_factory=list)  # as the model wrote it, before row-capping
    rejected_sql: list[str] = field(default_factory=list)

    def start(self, question: str) -> None:
        """Reset the per-run counters when a new user message arrives (adk web reuses one ledger)."""
        self.question = question
        self.tool_calls = self.prompt_tokens = self.output_tokens = self.model_calls = 0
        self.calls_by_tool = {}
        self.executed_sql = []
        self.rejected_sql = []

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

    def unexecuted_sql(self, answer: str) -> list[str]:
        """SQL blocks in the answer that no tool call ever ran."""
        ran = {normalize_sql(s) for s in self.executed_sql}
        return [b for b in sql_blocks(answer) if normalize_sql(b) not in ran]

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000
