"""Per-run bookkeeping: tool-call budget, token meter, evidence trail and answer verification.

One RunLedger is created per run and shared by every stage's callbacks, so the
CLI can report cost and verify the answer once the pipeline finishes.
"""

from __future__ import annotations

import builtins
import keyword
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

URL = re.compile(r"https?://[^\s)\]>\"'`|]+")
# Context7 library ids look like /owner/project or /websites/example_com (optionally /version).
LIBRARY_ID = re.compile(r"(?<![\w/.:-])(/(?:websites/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){0,2})(?=[\s)\]`,;|]|$)")
CODE_BLOCK = re.compile(r"```[\w+-]*\n(.*?)```", re.S)
REQUIRED_SECTIONS = ["## Answer", "## Sources"]

# Calls that appear in an answer's code but are not API claims about the library.
_IGNORED_CALLS = set(dir(builtins)) | set(keyword.kwlist) | {"self", "cls", "main"}


def question_from_message(text: str) -> str | None:
    """The user's question is the whole message; a greeting alone is not a question."""
    text = (text or "").strip()
    if len(text) < 12 or ("?" not in text and len(text.split()) < 4):
        return None
    return text


@dataclass
class RunLedger:
    model: str
    question: str = ""
    max_tool_calls: int = 10
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    evidence: list[str] = field(default_factory=list)

    def start(self, question: str) -> None:
        """Begin a new question. `adk web` reuses one ledger for every message, so budget and evidence reset here."""
        self.question = question
        self.tool_calls = 0
        self.calls_by_tool = {}
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.model_calls = 0
        self.evidence = []

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
    """Compare URLs without trailing punctuation or #fragment (an anchor into a page the tool did return)."""
    return url.rstrip(".,;:").split("#", 1)[0].rstrip("/")


def missing_sections(answer: str) -> list[str]:
    return [s for s in REQUIRED_SECTIONS if s not in answer]


def is_unanswered(answer: str) -> bool:
    """True when the answerer declined because the retrieved docs did not cover the question."""
    return "do not cover this" in answer


def unverified_sources(answer: str, ledger: RunLedger) -> list[str]:
    """Source URLs and Context7 library ids cited in the answer that no tool ever returned."""
    seen = "\n".join(ledger.evidence)
    seen_urls = {_norm(u) for u in URL.findall(seen)}
    sources = answer.split("## Sources", 1)[1] if "## Sources" in answer else ""
    bad = [u for u in (_norm(u) for u in URL.findall(sources)) if u not in seen_urls]
    bad += [i for i in LIBRARY_ID.findall(sources) if i not in seen]
    return sorted(set(bad))


def called_names(code: str) -> set[str]:
    """Function and method names the snippet calls, minus anything it defines itself."""
    defined = set(re.findall(r"\b(?:def|class)\s+([A-Za-z_]\w*)", code))
    defined |= set(re.findall(r"^\s*([A-Za-z_]\w*)\s*(?::[^=\n]+)?=(?!=)", code, re.M))
    defined |= set(re.findall(r"\b(?:for|as|import)\s+([A-Za-z_]\w*)", code))
    for params in re.findall(r"\bdef\s+\w+\(([^)]*)\)", code):
        defined |= {p.split(":")[0].split("=")[0].strip().lstrip("*") for p in params.split(",")}
    calls = set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", code))
    return {c for c in calls - defined - _IGNORED_CALLS if len(c) > 2}


def ungrounded_calls(answer: str, ledger: RunLedger) -> list[str]:
    """API calls used in the answer's code blocks that never appear in any retrieved doc.

    This is the cheap deterministic check for the most common docs-QA failure: a plausible
    function or method that the model remembered from training but the docs do not contain.
    """
    seen = "\n".join(ledger.evidence)
    bad: set[str] = set()
    for block in CODE_BLOCK.findall(answer):
        bad |= {name for name in called_names(block) if name not in seen}
    return sorted(bad)
