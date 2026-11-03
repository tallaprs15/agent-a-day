"""Per-run bookkeeping, request parsing and the evidence checks.

The model compares the CV with the job description; code decides what counts. Every claim the
model makes must carry a quote, and each quote is checked against the text a tool really returned.
Scores and the verdict are computed here, never by the model.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Approximate list prices in USD per 1M tokens (input, output), for budgeting, not billing.
# For models not listed, set GEMINI_PRICE_IN / GEMINI_PRICE_OUT.
DEFAULT_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

IMPORTANCE_WEIGHT = {"must": 2.0, "nice": 1.0}
STATUS_CREDIT = {"met": 1.0, "partial": 0.5, "gap": 0.0}

MAX_DOC_CHARS = 20_000
MAX_REQUEST_CHARS = 600
READABLE_SUFFIXES = {".md", ".txt"}
MIN_REQUIREMENTS = 3

URL = re.compile(r"https?://[^\s<>\"')]+")
FILE = re.compile(r"(?<![\w/:.\-])[\w./\-]+\.(?:md|txt)\b", re.I)
CV_REF = re.compile(r"\b(?:cv|resume|résumé)\b[\s:=]{0,3}([\w./\-]+\.(?:md|txt))", re.I)
JOB_REF = re.compile(r"\b(?:job|jd|role|posting)\b[\s:=]{0,3}(https?://[^\s<>\"')]+|[\w./\-]+\.(?:md|txt))", re.I)

ASK_FOR_INPUTS = (
    "Tell me which CV and which job to compare, for example: "
    '"CV: cv.md  Job: job.md" (files in the sandbox folder) or "CV: cv.md  Job: https://example.com/jobs/123".'
)


def parse_request(text: str) -> tuple[str, str] | None:
    """Return (cv_ref, job_ref) from the user's message, or None when either is missing."""
    text = text[:MAX_REQUEST_CHARS]
    cv = CV_REF.search(text)
    job = JOB_REF.search(text)
    cv_ref = cv.group(1) if cv else None
    job_ref = job.group(1) if job else None
    if not job_ref:
        urls = URL.findall(text)
        job_ref = urls[0] if urls else None
    if not cv_ref or not job_ref:
        files = [f for f in FILE.findall(text)]
        if not cv_ref and files:
            cv_ref = files[0]
        if not job_ref:
            rest = [f for f in files if f != cv_ref]
            job_ref = rest[0] if rest else None
    if not cv_ref or not job_ref or cv_ref == job_ref:
        return None
    return cv_ref, job_ref.rstrip(".,;")


def resolve_in_sandbox(ref: str, sandbox: Path) -> Path | None:
    """Absolute path of `ref` if it is a .md/.txt file inside the sandbox, else None."""
    root = sandbox.resolve()
    path = Path(ref)
    path = (path if path.is_absolute() else root / path).resolve()
    if path.suffix.lower() not in READABLE_SUFFIXES:
        return None
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


@dataclass
class RunLedger:
    model: str
    max_tool_calls: int = 6
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    # filled after the matcher has run
    report: str = ""
    score: float | None = None
    counts: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

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

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000


# ---------------------------------------------------------------- evidence checks


def norm(text: str) -> str:
    """Lowercase and drop Markdown decoration and spacing, so a quote survives re-wrapping."""
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    text = re.sub(r"^\s*[-•]\s+", " ", text, flags=re.M)
    text = re.sub(r"[*_`#>|\[\]]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_in(quote: str, source: str) -> bool:
    q = norm(quote)
    return len(q) >= 8 and q in norm(source)


def numbers_in(text: str) -> set[str]:
    return set(re.findall(r"\d[\d,.]*\d|\d", text))


def invented_numbers(bullet: str, cv_text: str) -> list[str]:
    """Figures in a tailored bullet that do not appear anywhere in the CV."""
    known = numbers_in(cv_text)
    return sorted(n for n in numbers_in(bullet) if n not in known)


def check_analysis(analysis: dict, cv_text: str, jd_text: str):
    """Validate the model's analysis against the real documents.

    Returns (requirements, bullets, problems). A requirement whose JD quote is not in the job
    description is dropped; one claimed met/partial without a CV quote that is really in the CV
    is demoted to a gap; a bullet with an unsupported source or an invented figure is dropped.
    """
    problems: list[str] = []
    reqs: list[dict] = []
    for r in analysis.get("requirements", []):
        r = dict(r)
        label = repr(r.get("requirement", "?"))
        if not quote_in(r.get("jd_quote", ""), jd_text):
            problems.append(f"Requirement quote not found in the job description, dropped: {label}")
            continue
        if r.get("status") in ("met", "partial") and not quote_in(r.get("cv_evidence", ""), cv_text):
            problems.append(f"CV evidence not found in the CV, demoted to gap: {label}")
            r["status"] = "gap"
        if r.get("status") == "gap":
            r["cv_evidence"] = ""
        reqs.append(r)
    bullets: list[dict] = []
    for b in analysis.get("tailored_bullets", []):
        short = repr(b.get("bullet", "?")[:60])
        if not quote_in(b.get("cv_source", ""), cv_text):
            problems.append(f"Tailored bullet cites a CV line that does not exist, dropped: {short}")
            continue
        bad = invented_numbers(b.get("bullet", ""), cv_text)
        if bad:
            problems.append(f"Tailored bullet contains figures not in the CV ({', '.join(bad)}), dropped: {short}")
            continue
        bullets.append(b)
    return reqs, bullets, problems


def fit_score(reqs: list[dict]) -> float | None:
    """Weighted share of requirements met, 0-100. Must-haves count double."""
    total = sum(IMPORTANCE_WEIGHT.get(r.get("importance"), 1.0) for r in reqs)
    if not total:
        return None
    got = sum(IMPORTANCE_WEIGHT.get(r.get("importance"), 1.0) * STATUS_CREDIT.get(r.get("status"), 0.0) for r in reqs)
    return round(100 * got / total, 1)


def verdict(score: float | None, reqs: list[dict]) -> str:
    if score is None:
        return "Not enough requirements to judge"
    must_gaps = [r for r in reqs if r.get("importance") == "must" and r.get("status") == "gap"]
    if score >= 75 and not must_gaps:
        return "Strong match"
    if score >= 50:
        n = len(must_gaps)
        return "Partial match" + (f" ({n} must-have gap{'s' if n != 1 else ''})" if n else "")
    return "Weak match"


def _cell(text: str | None) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def render_report(role: str, fit_summary: str, reqs: list[dict], bullets: list[dict], score: float | None, problems: list[str]) -> str:
    icon = {"met": "✅ met", "partial": "🟡 partial", "gap": "❌ gap"}
    lines = [f"# CV vs job: {role or 'role'}", ""]
    lines.append(f"**Fit score: {score if score is not None else 'n/a'} / 100** · {verdict(score, reqs)}  ")
    lines.append("_Must-haves count double; met = 1, partial = 0.5, gap = 0. Computed by code, not by the model._")
    if fit_summary:
        lines += ["", fit_summary.strip()]
    lines += ["", "## Requirements", "", "| Requirement | Importance | Status | Evidence from the CV |", "|---|---|---|---|"]
    for r in reqs:
        ev = f"“{_cell(r.get('cv_evidence'))}”" if r.get("cv_evidence") else "none found"
        note = f" {_cell(r.get('note'))}" if r.get("note") else ""
        lines.append(f"| {_cell(r.get('requirement'))} | {r.get('importance')} | {icon.get(r.get('status'), r.get('status'))} | {ev}{note} |")
    gaps = [r for r in reqs if r.get("status") != "met"]
    lines += ["", "## Gaps to address", ""]
    lines += [f"- **{_cell(r.get('requirement'))}** ({r.get('importance')}, {r.get('status')})" for r in gaps] or ["- None."]
    lines += ["", "## Tailored bullets (reworded from real CV lines, no new facts)", ""]
    for b in bullets:
        lines.append(f"- {b.get('bullet', '').strip()}  \n  _from:_ “{_cell(b.get('cv_source'))}”")
    if not bullets:
        lines.append("- None passed verification.")
    if problems:
        lines += ["", "## Integrity notes", ""] + [f"- {p}" for p in problems]
    return "\n".join(lines) + "\n"
