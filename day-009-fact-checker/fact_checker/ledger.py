"""Per-run bookkeeping and the deterministic checks the critic relies on.

The ledger counts tool calls (total and per round), meters tokens, remembers every source that a tool
really returned, and validates a verdict report without asking a model.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import quote

# Approximate list prices in USD per 1M tokens (input, output), for budgeting, not billing.
# For models not listed, set GEMINI_PRICE_IN / GEMINI_PRICE_OUT.
DEFAULT_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

URL = re.compile(r"https?://[^\s)\]>\"'`<\\]+")
VERDICTS = ("Supported", "Refuted", "Mixed", "Unverifiable")
VERDICT_LINE = re.compile(r"\*\*Verdict:\s*(Supported|Refuted|Mixed|Unverifiable)\*\*", re.I)
CLAIM_HEADING = re.compile(r"^###\s+Claim\s+\d+", re.M)
MIN_CLAIM_CHARS = 15
MAX_CLAIM_CHARS = 1500
MAX_EVIDENCE_CHARS = 400_000
QUOTE = re.compile(r'^\s*>\s*["“](.+?)["”]\s*$', re.M)
MIN_QUOTE_CHARS = 12


def normalise(text: str) -> str:
    """Reduce text to lower-case letters and digits, so a quote compares by its words.

    Spacing, punctuation, brackets and JSON escaping differ between a tool result and a model's copy of it;
    the words must still match exactly.
    """
    text = text.replace("\\n", " ")
    return re.sub(r"[\W_]+", "", text.lower())


def clean_url(url: str) -> str:
    """Drop trailing punctuation and the #fragment so links compare by page."""
    return url.rstrip(".,;:").split("#")[0].rstrip("/")


def wikipedia_url(title: str) -> str:
    return "https://en.wikipedia.org/wiki/" + quote(title.strip().replace(" ", "_"), safe="_(),'")


@dataclass
class RunLedger:
    model: str
    max_tool_calls: int = 14
    max_calls_per_round: int = 7
    tool_calls: int = 0
    round_calls: int = 0
    rounds: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    seen_urls: set[str] = field(default_factory=set)
    evidence: str = ""  # normalise()d text of everything the tools returned, for quote checks

    def start_round(self) -> None:
        """Each checker pass gets a fresh per-round allowance, but the total budget is never reset."""
        self.rounds += 1
        self.round_calls = 0

    def record_tool_call(self, name: str) -> str | None:
        """Count a tool call. Returns None if allowed, else the reason it was refused."""
        if self.tool_calls >= self.max_tool_calls:
            return f"Total tool budget of {self.max_tool_calls} calls reached. Write your verdicts now."
        if self.round_calls >= self.max_calls_per_round:
            return f"This round's budget of {self.max_calls_per_round} tool calls is spent. Write your verdicts now."
        self.tool_calls += 1
        self.round_calls += 1
        self.calls_by_tool[name] = self.calls_by_tool.get(name, 0) + 1
        return None

    def record_usage(self, prompt: int | None, output: int | None) -> None:
        self.model_calls += 1
        self.prompt_tokens += prompt or 0
        self.output_tokens += output or 0

    def record_evidence(self, text: str) -> None:
        """Remember every URL that came back from a tool."""
        text = text.replace("\\/", "/")
        if len(self.evidence) < MAX_EVIDENCE_CHARS:
            self.evidence += normalise(text)
        self.seen_urls.update(clean_url(u) for u in URL.findall(text))

    def record_wikipedia_page(self, title: str) -> None:
        """A Wikipedia tool that returned an article counts as evidence for that article's URL."""
        self.seen_urls.add(clean_url(wikipedia_url(title)))

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000


def split_claims(report: str) -> list[str]:
    """The per-claim blocks of a report (everything from one '### Claim N' heading to the next heading)."""
    starts = [m.start() for m in CLAIM_HEADING.finditer(report)]
    ends = starts[1:] + [len(report)]
    blocks = []
    for s, e in zip(starts, ends):
        block = report[s:e]
        cut = block.find("\n## ")  # the Sources section ends the last claim block
        blocks.append(block if cut < 0 else block[:cut])
    return blocks


def unverified_citations(report: str, ledger: RunLedger) -> list[str]:
    """Links in the report that no tool ever returned (likely invented)."""
    cited = {clean_url(u) for u in URL.findall(report)}
    return sorted(u for u in cited if u not in ledger.seen_urls)


def report_problems(report: str, ledger: RunLedger, expected_claims: int | None = None) -> list[str]:
    """Deterministic problems with a verdict report. An empty list means the structure is sound."""
    problems: list[str] = []
    blocks = split_claims(report)
    if not blocks:
        return ["No '### Claim N' sections found."]
    if expected_claims is not None and len(blocks) != expected_claims:
        problems.append(f"Expected {expected_claims} claim sections, found {len(blocks)}.")
    for i, block in enumerate(blocks, 1):
        m = VERDICT_LINE.search(block)
        if not m:
            problems.append(f"Claim {i} has no '**Verdict: <Supported|Refuted|Mixed|Unverifiable>**' line.")
        elif m.group(1).lower() != "unverifiable" and not URL.search(block):
            problems.append(f"Claim {i} is {m.group(1)} but cites no source link.")
    for q in QUOTE.findall(report):
        if len(q) >= MIN_QUOTE_CHARS and normalise(q) not in ledger.evidence:
            problems.append(f'Quoted evidence not found in any tool result: "{q[:80]}". Quote a span of 5 to 20 words copied character for character from a tool result.')
    if "## Sources" not in report:
        problems.append("Missing '## Sources' section.")
    bad = unverified_citations(report, ledger)
    if bad:
        problems.append("Links that no tool returned (remove or replace): " + ", ".join(bad))
    return problems


def count_claims(text: str) -> int:
    """Rough number of claims in the user's message: one per non-empty line, bullets stripped."""
    lines = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", ln).strip() for ln in text.splitlines()]
    return len([ln for ln in lines if ln])
