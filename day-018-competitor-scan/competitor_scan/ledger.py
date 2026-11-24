"""Per-run bookkeeping and the deterministic checks that make the final table trustworthy.

The ledger counts tool calls (total and per tool), meters tokens, and keeps every page passage a tool
returned under a numbered id. The model cites evidence by id instead of retyping it, so quotes in the final
table are copied by code and cannot be paraphrased or invented. Validation needs no model.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

# Approximate list prices in USD per 1M tokens (input, output), for budgeting, not billing.
# For models not listed, set GEMINI_PRICE_IN / GEMINI_PRICE_OUT.
DEFAULT_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

URL = re.compile(r"https?://[^\s)\]>\"'`<\\]+")
MIN_PRODUCTS = 2
MAX_PRODUCTS = 8
MIN_PASSAGE_CHARS = 12
MAX_PASSAGE_CHARS = 280
MAX_PASSAGES_PER_PAGE = 90
PRICE = re.compile(r"[$€£]\s?\d[\d,.]*")
PASSAGE_ID = re.compile(r"P\d+")

NON_VENDOR_HOSTS = {"github.com", "gitlab.com", "reddit.com", "medium.com", "news.ycombinator.com", "stackoverflow.com", "dev.to", "wikipedia.org", "en.wikipedia.org", "g2.com", "capterra.com"}
NON_VENDOR_PATH = re.compile(r"/(blog|compare|comparison|vs|alternatives?|articles?|news|best-[a-z-]+)(/|$)|-vs-|best-")

PricingModel = Literal["Free", "Freemium", "Paid", "Open source", "Contact sales", "Unknown"]
YesNo = Literal["Yes", "No", "Unknown"]


class Product(BaseModel):
    """One normalised row. Every field has a fixed vocabulary or a fixed shape, so rows compare."""

    name: str = Field(description="Product name, as the vendor writes it.")
    url: str = Field(description="URL of the page the passages came from (code replaces it with the passage's real page).")
    what_it_is: str = Field(description="One sentence, at most 20 words.")
    pricing_model: PricingModel
    starting_price: str = Field(description="Cheapest paid tier as written on the page, e.g. '$20/user/mo'; 'Free' or 'Unknown'.")
    open_source: YesNo
    differentiator: str = Field(description="What sets it apart, at most 15 words; 'Unknown' if the sources do not say.")
    what_it_is_passage: str = Field(description="Id of the page passage that supports what_it_is, e.g. 'P3'.")
    pricing_passage: str = Field(description="Id of the page passage that supports the pricing fields, e.g. 'P7'; empty only if pricing_model is Unknown.")


class Landscape(BaseModel):
    market: str
    products: list[Product]
    caveats: list[str] = Field(default_factory=list, description="Gaps in the evidence, e.g. pricing pages that were not reachable.")


def normalise(text: str) -> str:
    """Reduce text to lower-case letters and digits, so a quote compares by its words.

    Spacing, punctuation and JSON escaping differ between a tool result and a model's copy of it
    (and some search snippets arrive with their spaces stripped); the characters must still match.
    """
    text = text.replace("\\n", " ")
    return re.sub(r"[\W_]+", "", text.lower())


def clean_url(url: str) -> str:
    """Drop trailing punctuation, the #fragment and a trailing slash so links compare by page."""
    return url.strip().rstrip(".,;:").split("#")[0].rstrip("/")


def host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


@dataclass
class RunLedger:
    model: str
    max_tool_calls: int = 14
    max_searches: int = 3
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    seen_urls: set[str] = field(default_factory=set)  # URLs a search returned
    seen_hosts: set[str] = field(default_factory=set)
    requested_urls: set[str] = field(default_factory=set)  # every fetch attempt, successful or not
    passages: dict[str, tuple[str, str]] = field(default_factory=dict)  # id -> (page URL, text)

    def reset(self) -> None:
        """Start a new run (the playground reuses one ledger across messages)."""
        fresh = RunLedger(model=self.model, max_tool_calls=self.max_tool_calls, max_searches=self.max_searches)
        self.__dict__.update(fresh.__dict__)

    # -- budget -----------------------------------------------------------------------------------
    def record_tool_call(self, name: str) -> str | None:
        """Count a tool call. Returns a refusal message when the call must not run."""
        if self.tool_calls >= self.max_tool_calls:
            return f"Tool budget of {self.max_tool_calls} calls reached. Work with what you have."
        if name == "search" and self.calls_by_tool.get("search", 0) >= self.max_searches:
            return f"Search budget of {self.max_searches} reached. Fetch pages or write up what you have."
        self.tool_calls += 1
        self.calls_by_tool[name] = self.calls_by_tool.get(name, 0) + 1
        return None

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

    # -- evidence ---------------------------------------------------------------------------------
    def record_search(self, raw: str) -> None:
        for url in URL.findall(raw):
            self.seen_urls.add(clean_url(url))
            self.seen_hosts.add(host(url))

    def record_page(self, url: str, text: str) -> list[tuple[str, str]]:
        """Split a fetched page into numbered passages; returns [(id, text)] for the model to cite."""
        out = []
        for passage in split_passages(text)[:MAX_PASSAGES_PER_PAGE]:
            pid = f"P{len(self.passages) + 1}"
            self.passages[pid] = (clean_url(url), passage)
            out.append((pid, passage))
        return out

    @staticmethod
    def is_vendor_page(url: str) -> bool:
        """Vendor sites only: not code hosts, forums, or blog and comparison articles about vendors."""
        parsed = urlparse(url)
        if host(url) in NON_VENDOR_HOSTS:
            return False
        return not NON_VENDOR_PATH.search(parsed.path.lower())

    def already_requested(self, url: str) -> bool:
        return clean_url(url) in self.requested_urls

    def may_fetch(self, url: str) -> bool:
        """Only pages the search surfaced, or other pages on the same host (e.g. /pricing)."""
        return clean_url(url) in self.seen_urls or host(url) in self.seen_hosts

    def passage(self, pid: str, url: str) -> str | None:
        """The passage text for an id, only if it came from the given page."""
        page, text = self.passages.get(pid.strip().upper(), (None, None))
        return text if page == clean_url(url) else None

    def page_of(self, pid: str) -> str | None:
        """The page a passage id came from."""
        return self.passages.get(pid.strip().upper(), (None, None))[0]

    def has_url(self, url: str) -> bool:
        """True for a page that was actually read (so it has passages)."""
        return any(page == clean_url(url) for page, _ in self.passages.values())


def response_text(resp) -> str:
    """All text in an MCP tool response, whatever its wrapper (dict, list, content parts, string)."""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        return "\n".join(response_text(v) for k, v in resp.items() if k not in ("type", "annotations", "isError"))
    if isinstance(resp, (list, tuple)):
        return "\n".join(response_text(v) for v in resp)
    return ""


def split_passages(text: str) -> list[str]:
    """Break page text into sentence-sized passages worth citing, in page order, without duplicates."""
    out, seen = [], set()
    for line in re.split(r"\n+", text):
        for piece in re.split(r"(?<=[.!?])\s+(?=[A-Z$\"])", line.strip()):
            piece = re.sub(r"\s+", " ", piece).strip(" -*#>|")
            if len(piece) < MIN_PASSAGE_CHARS or normalise(piece) in seen:
                continue
            seen.add(normalise(piece))
            out.append(piece[:MAX_PASSAGE_CHARS])
    return out


# -- validation -----------------------------------------------------------------------------------
def row_problems(p: Product, ledger: RunLedger) -> list[str]:
    """Why a row cannot go into the table at all. Empty means the product is backed by a page that was read."""
    if ledger.page_of(p.what_it_is_passage) is None:
        return [f"{p.name}: description passage '{p.what_it_is_passage}' does not exist"]
    return []


def pricing_is_unsupported(p: Product, ledger: RunLedger) -> bool:
    """Pricing must cite a passage of the product's own page; any price and any free/open-source label must appear in it."""
    if p.pricing_model == "Unknown":
        return False
    text = ledger.passage(p.pricing_passage, p.url)
    if text is None:
        return True
    flat = normalise(text)
    if any(normalise(amount) not in flat for amount in PRICE.findall(p.starting_price)):
        return True
    # A "free" or "open source" label needs the cited passage to say so.
    needs = {"Free": "free", "Freemium": "free", "Open source": "opensource"}.get(p.pricing_model)
    return bool(needs and needs not in flat)


def split_rows(landscape: Landscape, ledger: RunLedger) -> tuple[list[Product], list[str]]:
    """Keep the verifiable, de-duplicated rows; explain every row that was dropped.

    A row whose product is real but whose pricing is not backed by its cited passage stays in the table
    with its pricing fields reset to Unknown: a missing price is honest, an unsupported one is not.
    """
    kept, dropped, seen = [], [], set()
    for p in landscape.products:
        key = normalise(p.name)
        if key in seen:
            dropped.append(f"{p.name}: duplicate row")
            continue
        problems = row_problems(p, ledger)
        if problems:
            dropped.extend(problems)
            continue
        seen.add(key)
        # The URL comes from the cited passage, not from what the model typed.
        p = p.model_copy(update={"url": ledger.page_of(p.what_it_is_passage)})
        if pricing_is_unsupported(p, ledger):
            dropped.append(f"{p.name}: pricing not backed by its cited passage; shown as Unknown")
            p = p.model_copy(update={"pricing_model": "Unknown", "starting_price": "Unknown", "pricing_passage": ""})
        kept.append(p)
    return kept[:MAX_PRODUCTS], dropped


def _cell(text: str) -> str:
    return re.sub(r"\s+", " ", text).replace("|", "/").strip() or "Unknown"


def render_table(landscape: Landscape, kept: list[Product], dropped: list[str], ledger: RunLedger) -> str:
    lines = [
        f"# Competitor landscape: {landscape.market.strip() or 'market'}",
        "",
        "| Product | What it is | Pricing model | Starting price | Open source | Differentiator |",
        "|---|---|---|---|---|---|",
    ]
    for p in kept:
        lines.append(
            f"| [{_cell(p.name)}]({clean_url(p.url)}) | {_cell(p.what_it_is)} | {p.pricing_model} | "
            f"{_cell(p.starting_price)} | {p.open_source} | {_cell(p.differentiator)} |"
        )
    lines += ["", "## Evidence", ""]
    for p in kept:
        lines.append(f"- **{_cell(p.name)}** ({p.what_it_is_passage.upper()}): \"{ledger.passage(p.what_it_is_passage, p.url)}\"")
        if p.pricing_model != "Unknown":
            lines.append(f"  - pricing ({p.pricing_passage.upper()}): \"{ledger.passage(p.pricing_passage, p.url)}\"")
    if landscape.caveats or dropped:
        lines += ["", "## Caveats", ""]
        lines += [f"- {c}" for c in landscape.caveats]
        lines += [f"- Not verified against tool output: {d}" for d in dropped]
    lines += ["", "## Sources", ""] + [f"- {u}" for u in dict.fromkeys(clean_url(p.url) for p in kept)]
    return "\n".join(lines) + "\n"


def table_problems(report: str, ledger: RunLedger) -> list[str]:
    """Checks on the rendered report itself (what the CLI and the eval dashboard see)."""
    problems = []
    rows = [ln for ln in report.splitlines() if ln.startswith("| [")]
    if len(rows) < MIN_PRODUCTS:
        problems.append(f"only {len(rows)} verified product row(s); need at least {MIN_PRODUCTS}")
    # Links the table and the Sources list make. URLs that merely appear inside a quoted passage are page text.
    cited = [ln for ln in report.splitlines() if ln.startswith(("| [", "- http"))]
    for url in sorted({clean_url(u) for ln in cited for u in URL.findall(ln)}):
        if not ledger.has_url(url) and url not in ledger.seen_urls:
            problems.append(f"URL in the report was not returned by any tool: {url}")
    return problems
