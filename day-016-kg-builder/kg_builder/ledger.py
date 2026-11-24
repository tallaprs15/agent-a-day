"""Per-run bookkeeping plus the code that decides what is allowed into the graph.

The model proposes entities and relations, each with a verbatim quote from the page it read.
Code keeps a proposal only if that quote really occurs in a page a tool returned, the name is in the
quote, and (for relations) both endpoints are kept entities. Everything written to the graph comes from
the kept set, never from what the model says later.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

# Approximate list prices in USD per 1M tokens (input, output), for budgeting, not billing.
# For models not listed, set GEMINI_PRICE_IN / GEMINI_PRICE_OUT.
DEFAULT_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

MAX_URLS = 3
MAX_PAGE_CHARS = 12_000  # per fetch; three pages are about 9k tokens
MAX_ENTITIES = 25
MAX_RELATIONS = 30
MAX_NAME_CHARS = 60
MAX_DESCRIPTION_CHARS = 160
MAX_QUOTE_CHARS = 300
MIN_QUOTE_CHARS = 12
ENTITY_TYPES = ("person", "organization", "product", "technology", "concept", "place", "event", "work")

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
RELATION_RE = re.compile(r"^[a-z][a-z_]{1,39}$")


def urls_from_message(text: str) -> list[str]:
    """Distinct https URLs in the user's message, in order, at most MAX_URLS. Trailing punctuation is dropped."""
    out: list[str] = []
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:!?")
        if urlparse(url).scheme == "https" and urlparse(url).hostname and url not in out:
            out.append(url)
    return out[:MAX_URLS]


def squash(text: str) -> str:
    """Normal form for quote matching: markdown links reduced to their text, punctuation and case ignored."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text or "")
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.lower()).split())


def normalise_relation(label: str) -> str:
    return re.sub(r"[^a-z]+", "_", str(label).strip().lower()).strip("_")


def response_text(response) -> str:
    """The plain text an MCP tool returned (not json.dumps: escaped newlines would break matching)."""
    if isinstance(response, dict):
        parts = [c.get("text", "") for c in response.get("content") or [] if isinstance(c, dict)]
        if parts:
            return "\n".join(parts)
        if "result" in response:
            return response_text(response["result"])
    if isinstance(response, str):
        return response
    return json.dumps(response, default=str, ensure_ascii=False)


def response_json(response):
    """The JSON object a memory-server tool returned, or None."""
    if isinstance(response, dict) and isinstance(response.get("structuredContent"), dict):
        return response["structuredContent"]
    try:
        return json.loads(response_text(response))
    except ValueError:
        return None


@dataclass
class Entity:
    name: str
    type: str
    description: str
    quote: str
    source: str

    @property
    def key(self) -> str:
        return self.name.casefold()

    def observations(self) -> list[str]:
        return [self.description, f"evidence: {self.quote}", f"source: {self.source}"]


@dataclass
class Relation:
    source_name: str
    target_name: str
    type: str
    quote: str
    source: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source_name.casefold(), self.target_name.casefold(), self.type)


@dataclass
class RunLedger:
    model: str
    max_tool_calls: int = 10
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    urls: list[str] = field(default_factory=list)
    pages: dict[str, str] = field(default_factory=dict)
    fetched: set[str] = field(default_factory=set)
    entities: list[Entity] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    gate_blocks: list[str] = field(default_factory=list)
    created_entities: set[str] = field(default_factory=set)
    created_relations: set[tuple[str, str, str]] = field(default_factory=set)
    persisted_entities: set[str] = field(default_factory=set)
    persisted_relations: set[tuple[str, str, str]] = field(default_factory=set)
    verified: bool = False
    report: str = ""

    def record_tool_call(self, name: str) -> bool:
        """Count a tool call. Returns False when the budget is exhausted."""
        if self.tool_calls >= self.max_tool_calls:
            return False
        self.tool_calls += 1
        self.calls_by_tool[name] = self.calls_by_tool.get(name, 0) + 1
        return True

    def reset_run(self, urls: list[str]) -> None:
        """Start a new request (a new chat message in adk web): clear per-request state, keep the token meter."""
        self.tool_calls, self.calls_by_tool = 0, {}
        self.urls, self.pages, self.fetched = list(urls), {}, set()
        self.entities, self.relations, self.dropped, self.gate_blocks = [], [], [], []
        self.created_entities, self.created_relations = set(), set()
        self.persisted_entities, self.persisted_relations = set(), set()
        self.verified, self.report = False, ""

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

    # --- grounding -------------------------------------------------------------------------

    def pages_text(self) -> str:
        return "\n\n".join(f"=== SOURCE: {url} ===\n{text}" for url, text in self.pages.items())

    def validate(self, raw: dict | None) -> None:
        """Keep the grounded part of the model's extraction; record why the rest was dropped."""
        raw = raw or {}
        squashed = {url: squash(text) for url, text in self.pages.items()}
        self.entities, self.relations, self.dropped = [], [], []

        def grounded(quote: str, source: str, *names: str) -> str | None:
            """Why the quote is not acceptable, or None when it is."""
            q = squash(quote)
            if source not in squashed:
                return f"source {source!r} was not fetched"
            if len(quote.strip()) < MIN_QUOTE_CHARS:
                return "quote too short"
            if q not in squashed[source]:
                return "quote not found in the page"
            for name in names:
                if squash(name) not in q:
                    return f"{name!r} is not in the quote"
            return None

        seen: set[str] = set()
        for item in (raw.get("entities") or [])[: MAX_ENTITIES * 2]:
            item = item if isinstance(item, dict) else dict(item)
            name = " ".join(str(item.get("name", "")).split())[:MAX_NAME_CHARS]
            kind = str(item.get("type", "")).strip().lower()
            quote = " ".join(str(item.get("quote", "")).split())[:MAX_QUOTE_CHARS]
            source = str(item.get("source", "")).strip()
            description = " ".join(str(item.get("description", "")).split())[:MAX_DESCRIPTION_CHARS]
            label = f"entity {name or '?'}"
            if not name:
                self.dropped.append(f"{label}: empty name")
            elif kind not in ENTITY_TYPES:
                self.dropped.append(f"{label}: type {kind!r} is not allowed")
            elif name.casefold() in seen:
                self.dropped.append(f"{label}: duplicate")
            elif len(self.entities) >= MAX_ENTITIES:
                self.dropped.append(f"{label}: over the {MAX_ENTITIES} entity cap")
            elif why := grounded(quote, source, name):
                self.dropped.append(f"{label}: {why}")
            else:
                seen.add(name.casefold())
                self.entities.append(Entity(name, kind, description or f"{kind}: {name}", quote, source))

        by_key = {e.key: e for e in self.entities}
        rel_seen: set[tuple[str, str, str]] = set()
        for item in (raw.get("relations") or [])[: MAX_RELATIONS * 2]:
            item = item if isinstance(item, dict) else dict(item)
            src = " ".join(str(item.get("source_entity", "")).split())
            dst = " ".join(str(item.get("target_entity", "")).split())
            rtype = normalise_relation(item.get("relation", ""))
            quote = " ".join(str(item.get("quote", "")).split())[:MAX_QUOTE_CHARS]
            source = str(item.get("source", "")).strip()
            label = f"relation {src or '?'} -{rtype or '?'}-> {dst or '?'}"
            if src.casefold() not in by_key or dst.casefold() not in by_key:
                self.dropped.append(f"{label}: an endpoint is not a kept entity")
            elif src.casefold() == dst.casefold():
                self.dropped.append(f"{label}: self relation")
            elif not RELATION_RE.match(rtype):
                self.dropped.append(f"{label}: relation label is not snake_case words")
            elif len(self.relations) >= MAX_RELATIONS:
                self.dropped.append(f"{label}: over the {MAX_RELATIONS} relation cap")
            elif why := grounded(quote, source, by_key[src.casefold()].name, by_key[dst.casefold()].name):
                self.dropped.append(f"{label}: {why}")
            else:
                rel = Relation(by_key[src.casefold()].name, by_key[dst.casefold()].name, rtype, quote, source)
                if rel.key not in rel_seen:
                    rel_seen.add(rel.key)
                    self.relations.append(rel)

    # --- what the memory server was asked to store -----------------------------------------

    def plan_payload(self) -> dict:
        """The exact arguments the writer may send, in the memory server's own field names."""
        return {
            "entities": [
                {"name": e.name, "entityType": e.type, "observations": e.observations()} for e in self.entities
            ],
            "relations": [
                {"from": r.source_name, "to": r.target_name, "relationType": r.type} for r in self.relations
            ],
        }

    def gate_entities(self, entities: list) -> list[dict]:
        """Reduce the writer's create_entities argument to planned entities, with the planned content."""
        planned = {e.key: e for e in self.entities}
        out: list[dict] = []
        for item in entities or []:
            name = str((item or {}).get("name", "")).strip()
            entity = planned.get(name.casefold())
            if entity is None:
                self.gate_blocks.append(f"entity not in the plan: {name[:MAX_NAME_CHARS]!r}")
            elif entity.key not in {o["name"].casefold() for o in out}:
                out.append({"name": entity.name, "entityType": entity.type, "observations": entity.observations()})
        return out

    def gate_relations(self, relations: list) -> list[dict]:
        planned = {r.key: r for r in self.relations}
        out: list[dict] = []
        for item in relations or []:
            item = item or {}
            key = (
                str(item.get("from", "")).strip().casefold(),
                str(item.get("to", "")).strip().casefold(),
                normalise_relation(item.get("relationType", "")),
            )
            rel = planned.get(key)
            if rel is None:
                self.gate_blocks.append(f"relation not in the plan: {key[0][:30]!r} -{key[2][:20]}-> {key[1][:30]!r}")
            elif {"from": rel.source_name, "to": rel.target_name, "relationType": rel.type} not in out:
                out.append({"from": rel.source_name, "to": rel.target_name, "relationType": rel.type})
        return out

    def record_write(self, tool: str, response) -> None:
        """Learn what the memory server really did from its own responses."""
        data = response_json(response)
        if tool == "create_entities":
            items = data if isinstance(data, list) else (data or {}).get("entities") or (data or {}).get("result") or []
            self.created_entities |= {str(i.get("name", "")).casefold() for i in items if isinstance(i, dict)}
        elif tool == "create_relations":
            items = data if isinstance(data, list) else (data or {}).get("relations") or (data or {}).get("result") or []
            self.created_relations |= {
                (str(i.get("from", "")).casefold(), str(i.get("to", "")).casefold(), str(i.get("relationType", "")))
                for i in items
                if isinstance(i, dict)
            }
        elif tool == "open_nodes" and isinstance(data, dict):
            self.persisted_entities |= {str(e.get("name", "")).casefold() for e in data.get("entities") or [] if isinstance(e, dict)}
            self.persisted_relations |= {
                (str(r.get("from", "")).casefold(), str(r.get("to", "")).casefold(), str(r.get("relationType", "")))
                for r in data.get("relations") or []
                if isinstance(r, dict)
            }
            self.verified = True

    # --- derived facts ---------------------------------------------------------------------

    @property
    def known_entities(self) -> set[str]:
        """Planned entities the graph already had (the server skips duplicates by name)."""
        return {e.key for e in self.entities} - self.created_entities

    def missing_entities(self) -> list[str]:
        return [e.name for e in self.entities if e.key not in self.persisted_entities]

    def missing_relations(self) -> list[str]:
        return [f"{r.source_name} -{r.type}-> {r.target_name}" for r in self.relations if r.key not in self.persisted_relations]


def _mermaid_id(index: int) -> str:
    return f"n{index}"


def _label(text: str) -> str:
    return text.replace('"', "'").replace("\n", " ")


def render_report(ledger: RunLedger) -> str:
    """The final Markdown, built from kept and verified data only."""
    lines = [f"# Knowledge graph from {len(ledger.pages)} source{'s' if len(ledger.pages) != 1 else ''}", ""]
    lines += ["## Sources", ""]
    for url in ledger.urls:
        lines.append(f"- {url} " + ("(fetched)" if url in ledger.fetched else "(**not fetched**)"))
    if not ledger.entities:
        lines += ["", "No grounded entities could be extracted, so nothing was written to the graph.", ""]
    else:
        ids = {e.key: _mermaid_id(i) for i, e in enumerate(ledger.entities)}
        lines += ["", "## Entities", "", "| Entity | Type | Graph | Evidence |", "|---|---|---|---|"]
        for e in ledger.entities:
            state = "already known" if e.key in ledger.known_entities and ledger.verified else "new"
            if not ledger.verified:
                state = "unverified"
            lines.append(f"| {e.name} | {e.type} | {state} | \"{e.quote}\" |")
        lines += ["", "## Relations", ""]
        if ledger.relations:
            lines += ["| From | Relation | To | Evidence |", "|---|---|---|---|"]
            for r in ledger.relations:
                lines.append(f"| {r.source_name} | {r.type} | {r.target_name} | \"{r.quote}\" |")
        else:
            lines.append("None of the proposed relations were grounded in a quote.")
        lines += ["", "## Graph", "", "```mermaid", "flowchart LR"]
        for e in ledger.entities:
            lines.append(f'  {ids[e.key]}["{_label(e.name)}<br/>{e.type}"]')
        for r in ledger.relations:
            lines.append(f'  {ids[r.source_name.casefold()]} -- "{_label(r.type)}" --> {ids[r.target_name.casefold()]}')
        lines += ["```"]
    lines += ["", "## Store", ""]
    if ledger.verified:
        lines.append(
            f"Read back from the memory server: {len(ledger.entities) - len(ledger.missing_entities())}/{len(ledger.entities)} entities "
            f"and {len(ledger.relations) - len(ledger.missing_relations())}/{len(ledger.relations)} relations present "
            f"({len(ledger.created_entities & {e.key for e in ledger.entities})} entities newly created, {len(ledger.known_entities)} already known)."
        )
    else:
        lines.append("The graph was not read back, so the writes are unverified.")
    if ledger.dropped:
        lines += ["", f"Dropped {len(ledger.dropped)} ungrounded or invalid proposals:", ""]
        lines += [f"- {d}" for d in ledger.dropped[:15]]
    return "\n".join(lines) + "\n"
