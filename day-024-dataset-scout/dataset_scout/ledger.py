"""Per-run bookkeeping: tool-call budget, token meter, and the evidence trail.

The evidence trail is the set of dataset ids that a tool really returned. The guide may only cite those:
`unverified_datasets` finds ids in the final text that no tool ever returned (likely invented).
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

MIN_REQUEST_CHARS = 15
MAX_REQUEST_CHARS = 1500

# A dataset id as the Hub prints it: "### author/name" in search results, "# author/name" in details,
# or a link such as https://hf.co/datasets/author/name.
_ID = r"[\w.\-]+/[\w.\-]+"
EVIDENCE_HEADING = re.compile(rf"^#{{1,3}}\s+({_ID})\s*$", re.M)
EVIDENCE_LINK = re.compile(rf"(?:huggingface\.co|hf\.co)/datasets/({_ID})")
# What the guide may cite: Hub links and load_dataset("id") snippets.
CITED_LINK = EVIDENCE_LINK
CITED_LOAD = re.compile(rf"load_dataset\(\s*[\"']({_ID})[\"']")


def _clean(dataset_id: str) -> str:
    return dataset_id.rstrip(".,;:)").lower()


@dataclass
class RunLedger:
    model: str
    max_tool_calls: int = 10
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    seen_datasets: set[str] = field(default_factory=set)
    structure_calls: int = 0
    guide: str = ""

    def record_tool_call(self, name: str, args: dict | None = None) -> bool:
        """Count a tool call. Returns False when the budget is exhausted."""
        if self.tool_calls >= self.max_tool_calls:
            return False
        self.tool_calls += 1
        self.calls_by_tool[name] = self.calls_by_tool.get(name, 0) + 1
        if name == "hub_repo_details" and "dataset_structure" in ((args or {}).get("operations") or []):
            self.structure_calls += 1
        return True

    def record_usage(self, prompt: int | None, output: int | None) -> None:
        self.model_calls += 1
        self.prompt_tokens += prompt or 0
        self.output_tokens += output or 0

    def record_evidence(self, text: str) -> None:
        """Remember every dataset id that came back from a tool."""
        text = text.replace("\\n", "\n")
        self.seen_datasets.update(_clean(i) for i in EVIDENCE_HEADING.findall(text))
        self.seen_datasets.update(_clean(i) for i in EVIDENCE_LINK.findall(text))

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000


def cited_datasets(text: str) -> set[str]:
    """Dataset ids the guide links to or loads."""
    return {_clean(i) for i in CITED_LINK.findall(text)} | {_clean(i) for i in CITED_LOAD.findall(text)}


def unverified_datasets(text: str, ledger: RunLedger) -> list[str]:
    """Dataset ids cited in the guide that no tool ever returned."""
    return sorted(cited_datasets(text) - ledger.seen_datasets)


def truncate_response(response, limit: int):
    """Cap the text a tool hands back to the model, keeping the response shape (dict with content parts, or str)."""
    if isinstance(response, str):
        return response if len(response) <= limit else response[:limit] + "\n[truncated]"
    if isinstance(response, dict) and isinstance(response.get("content"), list):
        budget = limit
        parts = []
        for part in response["content"]:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text = part["text"]
                if len(text) > budget:
                    text = text[: max(budget, 0)] + "\n[truncated]"
                budget -= len(part["text"])
                part = {**part, "text": text}
            parts.append(part)
        return {**response, "content": parts}
    return response
