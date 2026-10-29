"""Per-run bookkeeping, URL safety and the deterministic checks on an audit report.

The ledger counts tool calls, meters tokens and remembers every number the Chrome tools returned, so a
report can be checked without asking a model: metric values must come from the trace and each rating must
match the published Core Web Vitals thresholds.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# Approximate list prices in USD per 1M tokens (input, output), for budgeting, not billing.
# For models not listed, set GEMINI_PRICE_IN / GEMINI_PRICE_OUT.
DEFAULT_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

# (good up to, needs-improvement up to); anything above is Poor. LCP/INP/FCP/TTFB in ms, CLS unitless.
THRESHOLDS = {
    "LCP": (2500, 4000),
    "INP": (200, 500),
    "CLS": (0.1, 0.25),
    "FCP": (1800, 3000),
    "TTFB": (800, 1800),
}
RATINGS = ("Good", "Needs improvement", "Poor")

MS_VALUE = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*ms\b", re.I)
CLS_VALUE = re.compile(r"CLS\b[^\d\n]{0,20}(\d*\.\d+|\d+)")
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
ROW = re.compile(r"^\|\s*\**(LCP|CLS|INP|FCP|TTFB)\b[^|]*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|", re.M)
MAX_EVIDENCE_CHARS = 400_000
URL = re.compile(r"https?://[^\s)\]>\"'`<\\]+")
BARE_DOMAIN = re.compile(r"\b((?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s]*)?)", re.I)
# Threshold values the report may quote when explaining a rating.
KNOWN_MS = {2500.0, 4000.0, 200.0, 500.0, 1800.0, 3000.0, 800.0}


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def rate(metric: str, value: float) -> str:
    good, ok = THRESHOLDS[metric]
    return RATINGS[0] if value <= good else RATINGS[1] if value <= ok else RATINGS[2]


def extract_url(text: str) -> str | None:
    """First URL (or bare domain) in a user message, with https assumed for a bare domain."""
    m = URL.search(text)
    if m:
        return m.group(0).rstrip(".,;:")
    m = BARE_DOMAIN.search(text)
    return "https://" + m.group(1).rstrip(".,;:") if m else None


def url_problem(url: str) -> str | None:
    """Why a URL must not be opened, or None. Literal hosts only; DNS is not resolved here."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return "That is not a valid URL."
    if parts.scheme not in ("http", "https"):
        return "Only http and https pages can be audited."
    if not host:
        return "The URL has no host."
    if parts.username or parts.password:
        return "URLs with embedded credentials are not allowed."
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")) or "." not in host and ":" not in host:
        return "Local and internal hosts are not allowed; give a public page."
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not ip.is_global:
        return "Private, loopback and reserved addresses are not allowed; give a public page."
    return None


@dataclass
class RunLedger:
    model: str
    max_tool_calls: int = 10
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    ms_values: list[float] = field(default_factory=list)
    cls_values: list[float] = field(default_factory=list)
    evidence_chars: int = 0

    def record_tool_call(self, name: str) -> str | None:
        """Count a tool call. Returns None if allowed, else the reason it was refused."""
        if self.tool_calls >= self.max_tool_calls:
            return f"Tool budget of {self.max_tool_calls} calls reached. Write the report now."
        self.tool_calls += 1
        self.calls_by_tool[name] = self.calls_by_tool.get(name, 0) + 1
        return None

    def record_usage(self, prompt: int | None, output: int | None) -> None:
        self.model_calls += 1
        self.prompt_tokens += prompt or 0
        self.output_tokens += output or 0

    def record_evidence(self, text: str) -> None:
        """Remember every millisecond value and CLS score a tool returned."""
        if self.evidence_chars >= MAX_EVIDENCE_CHARS:
            return
        self.evidence_chars += len(text)
        self.ms_values += [_num(m) for m in MS_VALUE.findall(text)]
        self.cls_values += [float(m) for m in CLS_VALUE.findall(text)]

    def has_ms(self, value: float) -> bool:
        return any(abs(value - v) <= 0.51 for v in self.ms_values)

    def has_cls(self, value: float) -> bool:
        return any(abs(value - v) <= 0.005 for v in self.cls_values)

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.getenv("GEMINI_PRICE_IN")), float(os.getenv("GEMINI_PRICE_OUT")))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000


REQUIRED_SECTIONS = ("## Core Web Vitals", "## Diagnosis", "## Recommendations")


def report_checks(report: str, ledger: RunLedger) -> dict[str, list[str]]:
    """Deterministic checks on a report. Maps check name to its problems; all lists empty means it passed."""
    structure = [f"Missing '{s}' section." for s in REQUIRED_SECTIONS if s not in report]
    trace: list[str] = []
    if ledger.calls_by_tool.get("performance_start_trace", 0) == 0:
        trace.append("No performance trace was recorded; there is nothing to report on.")
    elif ledger.calls_by_tool.get("performance_analyze_insight", 0) == 0:
        trace.append("No insight was analysed; call performance_analyze_insight at least once.")

    numbers: list[str] = []
    ratings: list[str] = []
    rows = ROW.findall(report)
    if not rows:
        structure.append("The Core Web Vitals table has no LCP, CLS, INP, FCP or TTFB rows.")
    for metric, value, rating in rows:
        m = NUMBER.search(value)
        if not m:  # "n/a", "not measured": nothing to verify
            continue
        v = _num(m.group(0))
        known = ledger.has_cls(v) if metric == "CLS" else ledger.has_ms(v)
        if not known:
            numbers.append(f"{metric} value {m.group(0)} does not appear in any tool result.")
            continue
        want = rate(metric, v)
        if want.lower() not in rating.lower():
            ratings.append(f"{metric} {m.group(0)} is rated '{rating}' but the threshold says {want}.")
    # Any other millisecond figure in the prose must come from a tool result too.
    in_rows = {m.group(0) for r in rows for m in [NUMBER.search(r[1])] if m}
    for raw in sorted(set(MS_VALUE.findall(report))):
        v = _num(raw)
        if raw not in in_rows and v not in KNOWN_MS and not ledger.has_ms(v):
            numbers.append(f"'{raw} ms' appears in the report but in no tool result (derived figures: say so, or drop them).")
    return {"structure": structure, "trace_recorded": trace, "numbers_verified": numbers, "ratings_correct": ratings}


def report_problems(report: str, ledger: RunLedger) -> list[str]:
    return [p for ps in report_checks(report, ledger).values() for p in ps]
