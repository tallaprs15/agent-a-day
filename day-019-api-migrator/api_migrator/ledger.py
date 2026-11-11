"""Per-run bookkeeping: tool-call budget, token meter, per-side evidence and plan verification.

One RunLedger is shared by every stage's callbacks. Evidence is kept per side ("old" docs vs
"new" docs), so the checks can ask: was this removed API really in the old docs, and does its
replacement really exist in the new docs?
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
LIBRARY_ID = re.compile(r"(?<![\w/.:-])(/(?:websites/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){0,2})(?=[\s)\]`,;|]|$)")
CODE_BLOCK = re.compile(r"```[\w+-]*\n(.*?)```", re.S)
BACKTICKED = re.compile(r"`([^`\n]+)`")
VERSION = re.compile(r"\bv?\d+(?:\.\d+){0,3}(?:\.x)?\b|\b(?:latest|current)\b", re.I)
REQUIRED_SECTIONS = ["## Summary", "## Breaking changes", "## Migration steps", "## Sources"]
SIDES = ("old", "new")

_IGNORED_CALLS = set(dir(builtins)) | set(keyword.kwlist) | {"self", "cls", "main"}


def request_from_message(text: str) -> str | None:
    """A migration request names a library and two versions ("from 2.1 to 12", "v1 to latest").

    A greeting, or a request with fewer than two versions, is not enough to diff two doc sets.
    """
    text = (text or "").strip()
    if len(text) < 12 or len(VERSION.findall(text)) < 2:
        return None
    return text


@dataclass
class RunLedger:
    model: str
    request: str = ""
    max_tool_calls: int = 8
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    evidence: dict[str, list[str]] = field(default_factory=lambda: {s: [] for s in SIDES})

    def start(self, request: str) -> None:
        """Begin a new request. `adk web` reuses one ledger for every message, so budget and evidence reset here."""
        self.request = request
        self.tool_calls = 0
        self.calls_by_tool = {}
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.model_calls = 0
        self.evidence = {s: [] for s in SIDES}

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

    def record_evidence(self, side: str, text: str) -> None:
        self.evidence[side].append(text)

    def seen(self, side: str | None = None) -> str:
        sides = SIDES if side is None else (side,)
        return "\n".join(t for s in sides for t in self.evidence[s])

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000


def side_of(agent_name: str) -> str | None:
    """Which doc set an agent retrieves: 'old_researcher' -> 'old'."""
    for side in SIDES:
        if agent_name.startswith(side):
            return side
    return None


def _norm(url: str) -> str:
    return url.rstrip(".,;:").split("#", 1)[0].rstrip("/")


def missing_sections(plan: str) -> list[str]:
    return [s for s in REQUIRED_SECTIONS if s not in plan]


def is_unanswered(plan: str) -> bool:
    """True when the differ declined because the retrieved docs did not cover the migration."""
    return "do not cover this" in plan


def unverified_sources(plan: str, ledger: RunLedger) -> list[str]:
    """Source URLs and Context7 library ids cited in the plan that no tool ever returned."""
    seen = ledger.seen()
    seen_urls = {_norm(u) for u in URL.findall(seen)}
    sources = plan.split("## Sources", 1)[1] if "## Sources" in plan else ""
    bad = [u for u in (_norm(u) for u in URL.findall(sources)) if u not in seen_urls]
    bad += [i for i in LIBRARY_ID.findall(sources) if i not in seen]
    return sorted(set(bad))


def _section(plan: str, heading: str) -> str:
    if heading not in plan:
        return ""
    body = plan.split(heading, 1)[1]
    return re.split(r"\n## ", body, maxsplit=1)[0]


def breaking_change_rows(plan: str) -> list[tuple[str, str]]:
    """(old cell, new cell) of each data row in the Breaking changes table: | Old API | New API | Evidence |"""
    rows = []
    for line in _section(plan, "## Breaking changes").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not line.strip().startswith("|"):
            continue
        if set(cells[0]) <= set("-: ") or cells[0].lower().startswith("old"):
            continue
        rows.append((cells[0], cells[1]))
    return rows


def ungrounded_symbols(plan: str, ledger: RunLedger) -> list[str]:
    """The diff check. Every `symbol` in the Old API column must appear in the OLD docs, and every
    one in the New API column in the NEW docs. A removed API the model invented, or a replacement it
    remembered from training but the new docs never mention, is reported as 'old:name' / 'new:name'.
    """
    old_text, new_text = ledger.seen("old"), ledger.seen("new")
    bad: set[str] = set()
    for old_cell, new_cell in breaking_change_rows(plan):
        bad |= {f"old:{s}" for s in BACKTICKED.findall(old_cell) if s not in old_text}
        bad |= {f"new:{s}" for s in BACKTICKED.findall(new_cell) if s not in new_text}
    return sorted(bad)


def called_names(code: str) -> set[str]:
    """Function and method names the snippet calls, minus anything it defines itself."""
    defined = set(re.findall(r"\b(?:def|class)\s+([A-Za-z_]\w*)", code))
    defined |= set(re.findall(r"^\s*([A-Za-z_]\w*)\s*(?::[^=\n]+)?=(?!=)", code, re.M))
    defined |= set(re.findall(r"\b(?:for|as|import)\s+([A-Za-z_]\w*)", code))
    for params in re.findall(r"\bdef\s+\w+\(([^)]*)\)", code):
        defined |= {p.split(":")[0].split("=")[0].strip().lstrip("*") for p in params.split(",")}
    calls = set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", code))
    return {c for c in calls - defined - _IGNORED_CALLS if len(c) > 2}


def ungrounded_calls(plan: str, ledger: RunLedger) -> list[str]:
    """API calls in the plan's code blocks that appear in neither doc set."""
    seen = ledger.seen()
    bad: set[str] = set()
    for block in CODE_BLOCK.findall(plan):
        bad |= {name for name in called_names(block) if name not in seen}
    return sorted(bad)
