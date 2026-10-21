"""Per-run bookkeeping: tool-call budget, token meter, evidence trail and digest verification.

One RunLedger is created per run and shared by the agent's callbacks. It also reads the
knowledge-graph file itself (not through the model) so the post-run checks compare the
digest with what memory *actually* held before the run and holds afterwards.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Approximate list prices in USD per 1M tokens (input, output), for budgeting,
# not billing. For models not listed, set GEMINI_PRICE_IN / GEMINI_PRICE_OUT.
DEFAULT_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

URL = re.compile(r"https?://[^\s)\]>\"'`|]+")
HN_ITEM = re.compile(r"news\.ycombinator\.com/item\?id=(\d+)")
STORY_PREFIX = "hn-"  # memory entity names are hn-<item id>
REQUIRED_SECTIONS = ["## Stories", "## Memory"]
MEMORY_TOOLS = {"open_nodes", "search_nodes", "read_graph"}
GREETINGS = {"hi", "hello", "hey", "yo", "thanks", "thank you", "ok", "okay"}


def request_from_message(text: str) -> str | None:
    """Any real request works ("top stories", "5 stories about databases"); a bare greeting does not."""
    text = (text or "").strip()
    if len(text) < 3 or text.lower().strip("!. ") in GREETINGS:
        return None
    return text


def memory_path() -> Path:
    return Path(os.getenv("HN_MEMORY_FILE") or Path(__file__).resolve().parent.parent / ".memory" / "hn-digest.jsonl")


def stored_story_ids(path: Path) -> set[str]:
    """HN item ids that the knowledge-graph file holds an entity for (the server stores one JSON object per line)."""
    ids: set[str] = set()
    if not path.exists():
        return ids
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        name = str(row.get("name", ""))
        if row.get("type") == "entity" and name.startswith(STORY_PREFIX):
            ids.add(name[len(STORY_PREFIX):])
    return ids


@dataclass
class RunLedger:
    model: str
    request: str = ""
    memory_file: Path = field(default_factory=memory_path)
    max_tool_calls: int = 8
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    article_fetches: int = 0
    evidence: list[str] = field(default_factory=list)
    known_before: set[str] = field(default_factory=set)

    def start(self, request: str) -> None:
        """Begin a new request. `adk web` reuses one ledger for every message, so budget, evidence and the memory snapshot reset here."""
        self.request = request
        self.tool_calls = 0
        self.calls_by_tool = {}
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.model_calls = 0
        self.article_fetches = 0
        self.evidence = []
        self.known_before = stored_story_ids(self.memory_file)

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

    @property
    def consulted_memory(self) -> bool:
        return any(name in self.calls_by_tool for name in MEMORY_TOOLS)


def _norm(url: str) -> str:
    """Compare URLs without trailing punctuation, #fragment or trailing slash."""
    return url.rstrip(".,;:").split("#", 1)[0].rstrip("/")


def missing_sections(digest: str) -> list[str]:
    return [s for s in REQUIRED_SECTIONS if s not in digest]


def stories_section(digest: str) -> str:
    """The text between '## Stories' and the next heading."""
    if "## Stories" not in digest:
        return ""
    return re.split(r"\n## ", digest.split("## Stories", 1)[1], maxsplit=1)[0]


def cited_ids(digest: str) -> set[str]:
    """HN item ids the digest links to via the discussion link of each story."""
    return set(HN_ITEM.findall(stories_section(digest)))


def is_nothing_new(digest: str) -> bool:
    return "nothing new" in digest.lower()


def unverified_links(digest: str, ledger: RunLedger) -> list[str]:
    """URLs in the stories section that no tool ever returned (likely invented)."""
    seen = {_norm(u) for u in URL.findall("\n".join(ledger.evidence))}
    # The Algolia response carries bare item ids, so an item link is verified by its id.
    seen_text = "\n".join(ledger.evidence)
    bad = []
    for url in {_norm(u) for u in URL.findall(stories_section(digest))}:
        item = HN_ITEM.search(url)
        if item and f'"objectID":"{item.group(1)}"' in seen_text.replace(" ", ""):
            continue
        if url not in seen:
            bad.append(url)
    return sorted(bad)


def repeated_stories(digest: str, ledger: RunLedger) -> list[str]:
    """Stories in the digest that memory already held before this run began."""
    return sorted(cited_ids(digest) & ledger.known_before)


def unsaved_stories(digest: str, ledger: RunLedger) -> list[str]:
    """Stories in the digest that are not in the memory file after the run."""
    return sorted(cited_ids(digest) - stored_story_ids(ledger.memory_file))
