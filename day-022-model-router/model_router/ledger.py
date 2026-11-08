"""Per-run bookkeeping: routing policy, tool-call budget, per-model token meter.

The router model only *scores* a request; this module turns the scores into a
route with plain arithmetic, so the decision is deterministic and testable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

LITE, STRONG = "lite", "strong"
SCORE_MAX = 3
DEFAULT_THRESHOLD = 5  # of a possible 9
MIN_REQUEST_CHARS = 8
MAX_REQUEST_CHARS = 4000
FETCH_MAX_LENGTH = 8000
URL = re.compile(r"https?://[^\s<>\")\]]+")

# Dimensions the router scores 0-3. Code adds them up.
DIMENSIONS = ("reasoning_depth", "multi_step", "precision_risk")


def threshold() -> int:
    return int(os.getenv("ROUTE_THRESHOLD", DEFAULT_THRESHOLD))


@dataclass
class Route:
    tier: str  # "lite" or "strong"
    total: int | None  # None when the router output was unusable
    scores: dict[str, int]
    reason: str


def decide_route(assessment: object, limit: int | None = None) -> Route:
    """Turn the router's scores into a route. Unusable output fails safe to the strong model."""
    limit = threshold() if limit is None else limit
    data = assessment if isinstance(assessment, dict) else {}
    try:
        scores = {d: int(data[d]) for d in DIMENSIONS}
        if any(not 0 <= v <= SCORE_MAX for v in scores.values()):
            raise ValueError("score out of range")
    except (KeyError, TypeError, ValueError):
        return Route(STRONG, None, {}, "router output unusable; failing safe to the strong model")
    total = sum(scores.values())
    tier = STRONG if total >= limit else LITE
    return Route(tier, total, scores, str(data.get("rationale", "")))


def check_fetch_url(url: object) -> str | None:
    """None if the URL may be fetched, otherwise why not (public http/https only)."""
    try:
        parsed = urlparse(str(url))
    except ValueError:
        return "invalid URL"
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        return "only http(s) URLs are allowed"
    private = (
        host in ("localhost", "0.0.0.0") or host.endswith((".local", ".internal", ".localhost"))
        or re.fullmatch(r"(127|10|0)\.[\d.]+|192\.168\.[\d.]+|169\.254\.[\d.]+|172\.(1[6-9]|2\d|3[01])\.[\d.]+", host)
        or ":" in host  # IPv6 literals
    )
    return "private or local addresses are not allowed" if private else None


@dataclass
class RunLedger:
    lite_model: str
    strong_model: str
    max_tool_calls: int = 6
    tool_calls: int = 0
    tool_errors: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    model_calls: int = 0
    calls_by_model: dict[str, int] = field(default_factory=dict)
    tokens_by_model: dict[str, list[int]] = field(default_factory=dict)  # model -> [prompt, output]
    route: Route | None = None
    worker: str | None = None  # tier that actually ran
    request: str = ""
    answer: str = ""

    @property
    def model(self) -> str:
        return self.strong_model if self.worker == STRONG else self.lite_model

    @property
    def prompt_tokens(self) -> int:
        return sum(t[0] for t in self.tokens_by_model.values())

    @property
    def output_tokens(self) -> int:
        return sum(t[1] for t in self.tokens_by_model.values())

    def record_tool_call(self, name: str) -> bool:
        """Count a tool call. Returns False when the budget is exhausted."""
        if self.tool_calls >= self.max_tool_calls:
            return False
        self.tool_calls += 1
        self.calls_by_tool[name] = self.calls_by_tool.get(name, 0) + 1
        return True

    def record_usage(self, model: str, prompt: int | None, output: int | None) -> None:
        self.model_calls += 1
        self.calls_by_model[model] = self.calls_by_model.get(model, 0) + 1
        tokens = self.tokens_by_model.setdefault(model, [0, 0])
        tokens[0] += prompt or 0
        tokens[1] += output or 0

    def _price(self, model: str) -> tuple[float, float] | None:
        key = "STRONG" if model == self.strong_model and model != self.lite_model else "LITE"
        pin, pout = os.getenv(f"GEMINI_PRICE_{key}_IN"), os.getenv(f"GEMINI_PRICE_{key}_OUT")
        return (float(pin), float(pout)) if pin and pout else None

    def cost_usd(self) -> float | None:
        """Estimated cost across every model used, or None unless prices are configured for all of them."""
        total = 0.0
        for model, (p, o) in self.tokens_by_model.items():
            price = self._price(model)
            if price is None:
                return None
            total += (p * price[0] + o * price[1]) / 1_000_000
        return total
