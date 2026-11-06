"""Per-run bookkeeping (budget, token meter, evidence) and the PR URL scope check.

One RunLedger is created per run and shared by the agent's callbacks, so the CLI can report cost and
verify the review once the run finishes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

DEFAULT_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

# https://github.com/<owner>/<repo>/pull/<number> (extra path or query is ignored)
PR_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
MAX_REQUEST_CHARS = 2000

# Files named in a diff: "diff --git a/x b/x" and "+++ b/x".
DIFF_FILE = re.compile(r"^(?:diff --git a/(\S+) b/(\S+)|\+\+\+ b/(\S+))", re.M)
# `path/to/file.py`, `file.py:42`, `file.py:42-50` inside backticks.
CODE_SPAN = re.compile(r"`([^`\s]+)`")
SOURCE_EXT = {
    "py", "js", "jsx", "ts", "tsx", "go", "rs", "java", "kt", "rb", "php", "c", "h", "cc", "cpp", "hpp", "cs",
    "swift", "sh", "sql", "md", "yml", "yaml", "toml", "json", "html", "css", "scss", "lock", "txt", "cfg",
}


def parse_pr_url(text: str) -> tuple[str, str, int] | None:
    match = PR_URL.search(text or "")
    return (match.group(1), match.group(2), int(match.group(3))) if match else None


def allowed_fetch_url(url: str, pr: tuple[str, str, int]) -> bool:
    """Scope check: only this pull request's page, diff or patch may be fetched (read-only, one PR)."""
    owner, repo, number = pr
    parsed = urlparse(str(url))
    if parsed.scheme != "https" or parsed.hostname not in ("github.com", "patch-diff.githubusercontent.com"):
        return False
    path = parsed.path.rstrip("/")
    if parsed.hostname == "patch-diff.githubusercontent.com":
        path = path.removeprefix("/raw")
    base = f"/{owner}/{repo}/pull/{number}".lower()
    return path.lower() in (base, base + ".diff", base + ".patch", base + "/files")


@dataclass
class RunLedger:
    model: str
    max_tool_calls: int = 6
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    seen_files: set[str] = field(default_factory=set)
    evidence_chars: int = 0

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
        """Remember every file path the tools showed us (from diff headers)."""
        self.evidence_chars += len(text)
        for groups in DIFF_FILE.findall(text.replace("\\n", "\n")):
            self.seen_files.update(g for g in groups if g)

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000


def cited_files(review: str) -> set[str]:
    """File-looking code spans in the review, with any :line suffix removed."""
    out = set()
    for span in CODE_SPAN.findall(review):
        path = re.sub(r":\d+(?:-\d+)?$", "", span.strip(".,;"))
        if re.fullmatch(r"[\w./@+-]+\.[A-Za-z0-9]{1,6}", path) and path.rsplit(".", 1)[-1].lower() in SOURCE_EXT:
            out.add(path)
    return out


def unseen_files(review: str, ledger: RunLedger) -> list[str]:
    """Files the review cites that never appeared in a diff the tools returned (likely invented)."""
    seen = ledger.seen_files
    missing = []
    for path in sorted(cited_files(review)):
        if not any(s == path or s.endswith("/" + path) or path.endswith("/" + s) for s in seen):
            missing.append(path)
    return missing
