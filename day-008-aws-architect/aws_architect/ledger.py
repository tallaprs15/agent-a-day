"""Per-run bookkeeping: tool-call budgets, token meter, evidence trail and output checks.

One RunLedger is shared by the three parallel reviewers and the merger. Budgets are
enforced per reviewer (so one branch cannot starve the others) and in total.
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

URL = re.compile(r"https?://[^\s)\]>\"'`<\\]+")
REQUIRED_SECTIONS = [
    "## Summary",
    "## Top priorities",
    "## Cost optimization",
    "## Security",
    "## Reliability",
    "## Trade-offs",
    "## Sources",
]
MIN_ARCHITECTURE_CHARS = 25


def clean_url(url: str) -> str:
    """Drop trailing punctuation and the #fragment so links compare by page."""
    return url.rstrip(".,;:").split("#")[0].rstrip("/")


@dataclass
class RunLedger:
    model: str
    max_tool_calls: int = 15
    max_calls_per_reviewer: int = 5
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    calls_by_agent: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    seen_urls: set[str] = field(default_factory=set)

    def record_tool_call(self, name: str, agent: str = "") -> str | None:
        """Count a tool call. Returns None if allowed, else the reason it was refused."""
        if self.tool_calls >= self.max_tool_calls:
            return f"Total tool budget of {self.max_tool_calls} calls reached. Write your findings now."
        if agent and self.calls_by_agent.get(agent, 0) >= self.max_calls_per_reviewer:
            return f"Your budget of {self.max_calls_per_reviewer} tool calls is spent. Write your findings now."
        self.tool_calls += 1
        self.calls_by_tool[name] = self.calls_by_tool.get(name, 0) + 1
        if agent:
            self.calls_by_agent[agent] = self.calls_by_agent.get(agent, 0) + 1
        return None

    def record_usage(self, prompt: int | None, output: int | None) -> None:
        self.model_calls += 1
        self.prompt_tokens += prompt or 0
        self.output_tokens += output or 0

    def record_evidence(self, text: str) -> None:
        """Remember every URL that came back from a tool."""
        text = text.replace("\\/", "/")
        self.seen_urls.update(clean_url(u) for u in URL.findall(text))

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000


def is_aws_doc_url(url: str) -> bool:
    return re.match(r"https?://docs\.aws\.amazon\.com/", url) is not None


def unverified_citations(report: str, ledger: RunLedger) -> list[str]:
    """docs.aws.amazon.com links in the report that no tool ever returned (likely invented)."""
    cited = {clean_url(u) for u in URL.findall(report)}
    return sorted(u for u in cited if is_aws_doc_url(u) and u not in ledger.seen_urls)


def missing_sections(report: str) -> list[str]:
    return [s for s in REQUIRED_SECTIONS if s not in report]
