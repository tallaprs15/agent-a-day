"""Per-run bookkeeping: tool-call budget, token meter, the commits in range and the map/reduce coverage checks.

One RunLedger is created per run and shared by the agents' callbacks, so the CLI can report cost and check that the
final notes cite exactly the commits the git server returned.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

# Approximate list prices in USD per 1M tokens (input, output), for budgeting,
# not billing. For models not listed, set GEMINI_PRICE_IN / GEMINI_PRICE_OUT.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {}

MAPPERS = 4  # fixed fan-out: the commits are split across this many parallel map workers
MAX_COMMITS = 60
SHA_LEN = 7
CITATION = re.compile(r"`([0-9a-f]{7})`")
GREETINGS = {"hi", "hello", "hey", "yo", "thanks", "thank you", "ok", "okay"}
COMMIT_TYPES = ("breaking", "feature", "fix", "performance", "docs", "internal")


def repo_from_message(text: str) -> str | None:
    """The repository path is the first path-looking token in the message ('notes for /tmp/x', '~/code/app')."""
    text = (text or "").strip()
    if len(text) < 2 or text.lower().strip("!. ") in GREETINGS:
        return None
    for token in text.replace("`", " ").split():
        token = token.strip("\"'.,;:()")
        if token.startswith(("/", "~", "./", "../")):
            return os.path.abspath(os.path.expanduser(token))
    return None


def extract_text(tool_response: Any) -> str:
    """Flatten an MCP tool response (dict with content parts, structured content or plain text) to text."""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        parts = tool_response.get("content")
        if isinstance(parts, list):
            return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if "result" in tool_response:
            return extract_text(tool_response["result"])
    return json.dumps(tool_response, default=str)


def parse_log(text: str) -> list[dict]:
    """Parse `git_log` output ("Commit: <sha>\\nAuthor: ..\\nDate: ..\\nMessage: ..") into commit dicts, newest first."""
    commits = []
    for block in re.split(r"(?m)^Commit: ", text)[1:]:
        match = re.match(r"([0-9a-f]{40})\nAuthor: (.*)\nDate: (.*)\nMessage: (.*)", block, re.S)
        if not match:
            continue
        sha, author, date, message = match.groups()
        message = message.strip()
        commits.append({"sha": sha, "short": sha[:SHA_LEN], "author": author.strip(), "date": date.strip(), "message": message})
    return commits


def chunk_commits(commits: list[dict], parts: int = MAPPERS) -> list[list[dict]]:
    """Split commits into `parts` contiguous, near-equal chunks (some may be empty when there are few commits)."""
    size, extra = divmod(len(commits), parts)
    chunks, start = [], 0
    for i in range(parts):
        end = start + size + (1 if i < extra else 0)
        chunks.append(commits[start:end])
        start = end
    return chunks


def render_chunk(chunk: list[dict], body_chars: int = 300) -> str:
    """Compact text for a map worker: one block per commit, message body clipped."""
    lines = []
    for c in chunk:
        subject, _, body = c["message"].partition("\n")
        body = " ".join(body.split())[:body_chars]
        lines.append(f"{c['short']} {c['date'][:10]} {subject}" + (f"\n    {body}" if body else ""))
    return "\n".join(lines)


def entries_of(state_value: Any) -> list[dict]:
    """Entries from a map worker's output_key value (a dict, a JSON string or a pydantic model)."""
    if hasattr(state_value, "model_dump"):
        state_value = state_value.model_dump()
    if isinstance(state_value, str):
        try:
            state_value = json.loads(state_value)
        except ValueError:
            return []
    entries = state_value.get("entries") if isinstance(state_value, dict) else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


@dataclass
class RunLedger:
    model: str
    repo: str = ""
    max_tool_calls: int = 3
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    commits: list[dict] = field(default_factory=list)

    def start(self, repo: str) -> None:
        """Reset the per-run state when a new request arrives (adk web reuses one ledger)."""
        self.repo = repo
        self.tool_calls = self.prompt_tokens = self.output_tokens = self.model_calls = 0
        self.calls_by_tool = {}
        self.commits = []

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
        price = (prices if prices is not None else DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000

    def add_log(self, log_text: str) -> list[dict]:
        """Record the commits from a git_log result (deduplicated by sha, order kept)."""
        known = {c["sha"] for c in self.commits}
        for commit in parse_log(log_text):
            if commit["sha"] not in known:
                known.add(commit["sha"])
                self.commits.append(commit)
        return self.commits


def check_run(notes: str, ledger: RunLedger, map_outputs: list[Any]) -> dict[str, Any]:
    """Deterministic checks across the map and reduce stages.

    * the git server returned commits
    * map: every commit got a classification and the workers invented none
    * reduce: the notes cite every commit by its 7-character sha and cite none that were not in the range
    """
    known = {c["short"] for c in ledger.commits}
    mapped = [e.get("sha") for out in map_outputs for e in entries_of(out)]
    cited = set(CITATION.findall(notes))
    return {
        "commits_collected": bool(known),
        "map_covers_every_commit": known <= set(mapped),
        "map_invented_nothing": set(mapped) <= known,
        "notes_cover_every_commit": known <= cited,
        "notes_invented_nothing": cited <= known,
        "unmapped_shas": sorted(known - set(mapped)),
        "uncited_shas": sorted(known - cited),
        "invented_shas": sorted((cited | set(mapped)) - known - {None}),
        "commits": len(known),
    }


CHECK_KEYS = ("commits_collected", "map_covers_every_commit", "map_invented_nothing", "notes_cover_every_commit", "notes_invented_nothing")


def passed(checks: dict[str, Any]) -> bool:
    return all(checks[k] for k in CHECK_KEYS)
