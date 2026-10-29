"""Per-run bookkeeping and the scoring rubric.

The rubric is code, not prose: the model judges only what needs judgement (task fit, constraint fit);
popularity, freshness and licence are computed here from facts that a tool really returned, and the
weighted total and the ordering are computed here too. The model never does arithmetic or sorts.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime

# Approximate list prices in USD per 1M tokens (input, output), for budgeting, not billing.
# For models not listed, set GEMINI_PRICE_IN / GEMINI_PRICE_OUT.
DEFAULT_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

# criterion -> (weight, who scores it). Weights sum to 1.
RUBRIC: dict[str, tuple[float, str]] = {
    "task_fit": (0.35, "model"),
    "constraint_fit": (0.25, "model"),
    "popularity": (0.15, "code"),
    "licence": (0.15, "code"),
    "freshness": (0.10, "code"),
}
MODEL_CRITERIA = [c for c, (_, who) in RUBRIC.items() if who == "model"]
MAX_SCORE = 5

PERMISSIVE_LICENCES = {"mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "cc-by-4.0", "cc0-1.0", "unlicense", "bsd"}

MIN_REQUEST_CHARS = 15
MAX_REQUEST_CHARS = 1500

REPO_HEADING = re.compile(r"^###\s+([\w.\-]+/[\w.\-]+)\s*$", re.M)
DETAILS_HEADING = re.compile(r"^#\s+([\w.\-]+/[\w.\-]+)\s*$", re.M)
DOWNLOADS = re.compile(r"\*\*Downloads:\*\*\s*([\d.,]+)\s*([KMB]?)", re.I)
LIKES = re.compile(r"\*\*Likes:\*\*\s*([\d,]+)")
LICENCE_TAG = re.compile(r"license:([\w.\-]+)", re.I)
LICENCE_FIELD = re.compile(r"\*\*License:\*\*\s*([\w.\-]+)", re.I)
DATE = re.compile(r"\*\*(Created|Updated):\*\*\s*(\d{1,2} [A-Z][a-z]{2}, \d{4})")


def parse_count(number: str, suffix: str) -> int:
    value = float(number.replace(",", ""))
    return int(value * {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suffix.upper()])


def parse_date(text: str) -> date | None:
    try:
        return datetime.strptime(text, "%d %b, %Y").date()
    except ValueError:
        return None


@dataclass
class RepoFacts:
    """What the Hub told us about one repo. Missing fields stay None, never guessed."""

    repo_id: str
    downloads: int | None = None
    likes: int | None = None
    licence: str | None = None
    created: date | None = None
    updated: date | None = None


def parse_facts(text: str) -> list[RepoFacts]:
    """Pull facts out of hub_repo_search / hub_repo_details markdown (one block per repo)."""
    text = text.replace("\\n", "\n").replace('\\"', '"')
    heads = sorted(
        [(m.start(), m.group(1)) for m in REPO_HEADING.finditer(text)]
        + [(m.start(), m.group(1)) for m in DETAILS_HEADING.finditer(text)]
    )
    out = []
    for i, (start, repo_id) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        block = text[start:end]
        facts = RepoFacts(repo_id=repo_id)
        if m := DOWNLOADS.search(block):
            facts.downloads = parse_count(m.group(1), m.group(2))
        if m := LIKES.search(block):
            facts.likes = int(m.group(1).replace(",", ""))
        if m := LICENCE_FIELD.search(block) or LICENCE_TAG.search(block):
            facts.licence = m.group(1).lower()
        for kind, when in DATE.findall(block):
            setattr(facts, kind.lower(), parse_date(when))
        out.append(facts)
    return out


def popularity_score(downloads: int | None) -> int:
    """0 at 100 downloads or fewer, 5 at 1M or more, log-scaled in between."""
    if not downloads or downloads <= 100:
        return 0
    return max(0, min(MAX_SCORE, round((math.log10(downloads) - 2) / 4 * MAX_SCORE)))


def freshness_score(last: date | None, today: date | None = None) -> int:
    """5 within 6 months, then 4/3/2/1 at 1/2/3 years and beyond; 0 when no date is known."""
    if last is None:
        return 0
    age_days = ((today or date.today()) - last).days
    for limit, score in ((183, 5), (365, 4), (730, 3), (1095, 2)):
        if age_days <= limit:
            return score
    return 1


def licence_score(licence: str | None) -> int:
    """5 permissive, 2 other declared licence (check the terms), 0 undeclared."""
    if not licence:
        return 0
    return 5 if licence in PERMISSIVE_LICENCES else 2


@dataclass
class RunLedger:
    model: str
    max_tool_calls: int = 10
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    facts: dict[str, RepoFacts] = field(default_factory=dict)
    ranked: list = field(default_factory=list)
    disqualified: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    report: str = ""

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
        """Merge facts from a tool result; a later, richer result fills gaps but never erases a value."""
        for new in parse_facts(text):
            old = self.facts.setdefault(new.repo_id.lower(), RepoFacts(new.repo_id))
            for name in ("downloads", "likes", "licence", "created", "updated"):
                if getattr(new, name) is not None:
                    setattr(old, name, getattr(new, name))

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000


@dataclass
class Scored:
    repo_id: str
    scores: dict[str, int]
    total: float
    rationale: str
    caveat: str
    facts: RepoFacts


def weighted_total(scores: dict[str, int]) -> float:
    """Weighted sum on a 0-100 scale."""
    return round(sum(RUBRIC[c][0] * scores[c] for c in RUBRIC) / MAX_SCORE * 100, 1)


def score_candidates(judged: list[dict], ledger: RunLedger, today: date | None = None) -> tuple[list[Scored], list[str], list[str]]:
    """Combine the model's judgements with code-computed criteria.

    Returns (ranked best first, disqualified repo ids, problems). A candidate the tools never returned is
    a problem, not a ranking entry: it cannot be scored from evidence.
    """
    ranked: list[Scored] = []
    out: list[str] = []
    problems: list[str] = []
    seen: set[str] = set()
    for item in judged:
        repo_id = str(item.get("repo_id", "")).strip()
        key = repo_id.lower()
        if key in seen:
            problems.append(f"{repo_id} is ranked twice.")
            continue
        seen.add(key)
        facts = ledger.facts.get(key)
        if facts is None:
            problems.append(f"{repo_id} was never returned by a tool, so it cannot be scored.")
            continue
        if item.get("disqualified"):
            out.append(facts.repo_id)
            continue
        scores = {c: int(item[c]) for c in MODEL_CRITERIA}
        if any(not 0 <= v <= MAX_SCORE for v in scores.values()):
            problems.append(f"{repo_id} has a score outside 0-{MAX_SCORE}.")
            continue
        scores["popularity"] = popularity_score(facts.downloads)
        scores["licence"] = licence_score(facts.licence)
        scores["freshness"] = freshness_score(facts.updated or facts.created, today)
        ranked.append(Scored(facts.repo_id, scores, weighted_total(scores), str(item.get("rationale", "")), str(item.get("caveat", "")), facts))
    ranked.sort(key=lambda s: (-s.total, s.repo_id.lower()))
    if not ranked:
        problems.append("No candidate survived scoring.")
    return ranked, out, problems


def render_report(request: str, ranked: list[Scored], disqualified: list[str]) -> str:
    """The final Markdown, built from code-computed numbers so the table can be trusted."""
    head = "| # | Model | Total | " + " | ".join(c.replace("_", " ") for c in RUBRIC) + " |"
    sep = "|---:|---|---:|" + "---:|" * len(RUBRIC)
    rows = [
        f"| {i} | [{s.repo_id}](https://huggingface.co/{s.repo_id}) | **{s.total}** | " + " | ".join(str(s.scores[c]) for c in RUBRIC) + " |"
        for i, s in enumerate(ranked, 1)
    ]
    lines = [
        "# Model shortlist",
        f"> {request.strip()}",
        "",
        "## Ranking",
        head,
        sep,
        *rows,
        "",
        "Scores are 0-5 per criterion; total is 0-100. "
        + ", ".join(f"{c.replace('_', ' ')} {int(w * 100)}% ({'model' if who == 'model' else 'computed from Hub data'})" for c, (w, who) in RUBRIC.items())
        + ".",
        "",
        "## Why",
    ]
    for i, s in enumerate(ranked, 1):
        f = s.facts
        facts = ", ".join(
            x
            for x in (
                f"{f.downloads:,} downloads" if f.downloads is not None else "downloads unknown",
                f"{f.likes:,} likes" if f.likes is not None else "",
                f"licence {f.licence}" if f.licence else "licence not declared",
                f"updated {f.updated or f.created}" if (f.updated or f.created) else "",
            )
            if x
        )
        lines += [f"### {i}. {s.repo_id}", f"_{facts}_", "", s.rationale.strip()]
        if s.caveat.strip():
            lines += ["", f"**Caveat:** {s.caveat.strip()}"]
        lines.append("")
    if disqualified:
        lines += ["## Ruled out", *[f"- {r}: breaks a hard constraint in the request" for r in disqualified], ""]
    return "\n".join(lines)
