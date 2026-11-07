"""Per-run bookkeeping (budget, token meter, evidence), the URL scope check and the answer validators.

One RunLedger is created per run and shared by the agent's callbacks, so the CLI can report cost and
verify the triage once the run finishes.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote_plus, urlencode, urlparse

DEFAULT_PRICES = {
    "gemini-3.5-flash-lite": (0.10, 0.40),
    "gemini-3.8-flash": (0.30, 2.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}

# https://github.com/<owner>/<repo>/issues/<number> (a /pull/<number> link is accepted too: GitHub numbers both
# from one sequence and serves pull requests through the issues API).
ISSUE_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/(?:issues|pull)/(\d+)")
MAX_REQUEST_CHARS = 2000
BODY_CHARS = 700  # per issue body kept after slimming
MAX_PER_PAGE = 20
MAX_LIST_PAGE = 3

Ref = tuple[str, str, int]


def parse_issue_url(text: str) -> Ref | None:
    match = ISSUE_URL.search(text or "")
    return (match.group(1), match.group(2), int(match.group(3))) if match else None


def allowed_fetch_url(url: str, ref: Ref) -> bool:
    """Scope check: read-only GET of one repository's issue data via the public REST API, nothing else.

    Allowed (all under https://api.github.com/repos/<owner>/<repo>/):
      issues/<n> and issues/<n>/comments   the target issue
      issues?state=...&per_page=...        recent issues (candidates for duplicates)
      labels                               the labels that exist (the only ones we may suggest)
    and https://api.github.com/search/issues?q=... where q is restricted to the same repository.
    """
    owner, repo, number = ref
    parsed = urlparse(str(url))
    if parsed.scheme != "https" or parsed.hostname != "api.github.com" or parsed.username or parsed.port:
        return False
    path = parsed.path.rstrip("/").lower()
    base = f"/repos/{owner}/{repo}".lower()
    query = parse_qs(parsed.query)
    if path == "/search/issues":
        q = unquote_plus(" ".join(query.get("q", [])))
        repos = re.findall(r"repo:(\S+)", q, flags=re.I)
        return bool(repos) and all(r.lower() == f"{owner}/{repo}".lower() for r in repos) and not re.search(
            r"\b(?:org|user|owner):", q, flags=re.I
        )
    return path in (f"{base}/issues/{number}", f"{base}/issues/{number}/comments", f"{base}/issues", f"{base}/labels")


def search_missing_type(url: str) -> bool:
    """GitHub's search API answers 422 unless the query says is:issue or is:pr; catch that before it costs a call."""
    parsed = urlparse(url)
    if parsed.path.rstrip("/").lower() != "/search/issues":
        return False
    q = unquote_plus(" ".join(parse_qs(parsed.query).get("q", [])))
    return not re.search(r"\bis:(?:issue|pr|pull-request)\b", q, flags=re.I)


def cap_query(url: str) -> str:
    """Return the URL with per_page and page capped, so one call can't pull a whole backlog."""
    parsed = urlparse(url)
    if not (parsed.path.endswith(("/issues", "/labels")) or parsed.path.endswith("/search/issues")):
        return url  # single issue or comments: nothing to cap
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["per_page"] = [str(min(int((query.get("per_page") or [MAX_PER_PAGE])[0] or MAX_PER_PAGE), MAX_PER_PAGE))]
    query["page"] = [str(min(int((query.get("page") or [1])[0] or 1), MAX_LIST_PAGE))]
    return parsed._replace(query=urlencode({k: v[0] for k, v in query.items()}, safe=":/+")).geturl()


def _login(user) -> str | None:
    return user.get("login") if isinstance(user, dict) else None


def _slim_issue(item: dict) -> dict:
    body = (item.get("body") or "").replace("\r", "")
    out = {
        "number": item.get("number"),
        "title": item.get("title"),
        "state": item.get("state"),
        "kind": "pull_request" if item.get("pull_request") else "issue",
        "labels": [l.get("name") for l in item.get("labels") or [] if isinstance(l, dict)],
        "author": _login(item.get("user")),
        "assignees": [a for a in map(_login, item.get("assignees") or []) if a],
        "created_at": item.get("created_at"),
        "body": body[:BODY_CHARS] + ("…" if len(body) > BODY_CHARS else ""),
    }
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def slim_response(text: str) -> str | None:
    """Reduce a GitHub REST JSON payload to the fields triage needs. Returns None if `text` isn't such JSON.

    Raw issue JSON is ~8 KB per issue (dozens of URL fields); this keeps ~0.5 KB, which lets the agent look at
    more candidates for the same tokens and keeps the evidence the checks use small and readable.
    """
    start = min((i for i in (text.find("["), text.find("{")) if i >= 0), default=-1)
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("items"), list):  # search/issues
        data = data["items"]
    if isinstance(data, dict) and "number" in data and "title" in data:
        data = [data]
    if not isinstance(data, list):
        return None
    slim = []
    for item in data:
        if not isinstance(item, dict):
            return None
        if "number" in item and "title" in item:
            slim.append(_slim_issue(item))
        elif "name" in item and "color" in item:  # label
            slim.append({k: item[k] for k in ("name", "description") if item.get(k)})
        elif "body" in item and "user" in item:  # issue comment
            slim.append({"comment_by": _login(item.get("user")), "body": (item.get("body") or "")[:BODY_CHARS]})
        else:
            return None
    return json.dumps(slim, ensure_ascii=False, indent=1)


@dataclass
class RunLedger:
    model: str
    max_tool_calls: int = 8
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    target_seen: bool = False
    labels_listed: bool = False
    seen_issues: set[int] = field(default_factory=set)
    repo_labels: set[str] = field(default_factory=set)
    seen_logins: set[str] = field(default_factory=set)
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

    def record_evidence(self, slim_json: str, target: int | None = None) -> None:
        """Remember which issues, labels and people the tools actually showed us (from the slimmed payload)."""
        self.evidence_chars += len(slim_json)
        try:
            items = json.loads(slim_json)
        except ValueError:
            return
        for item in items if isinstance(items, list) else []:
            if "number" in item:
                self.seen_issues.add(int(item["number"]))
                if target is not None and item["number"] == target:
                    self.target_seen = True
                self.seen_logins.update(a for a in [item.get("author"), *item.get("assignees", [])] if a)
            elif "comment_by" in item:
                self.seen_logins.add(item["comment_by"])
            elif "name" in item:
                self.labels_listed = True
                self.repo_labels.add(item["name"])

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000


def section(text: str, heading: str) -> str:
    """Body of a '## <heading>' section (up to the next '## ')."""
    match = re.search(rf"^##\s+{re.escape(heading)}[^\n]*\n(.*?)(?=^##\s|\Z)", text, flags=re.M | re.S | re.I)
    return match.group(1) if match else ""


def suggested_labels(triage: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"`([^`\n]+)`", section(triage, "Labels"))))


def cited_duplicates(triage: str) -> list[int]:
    return list(dict.fromkeys(int(n) for n in re.findall(r"#(\d+)", section(triage, "Possible duplicates"))))


def cited_owners(triage: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"@([A-Za-z0-9][A-Za-z0-9-]*)", section(triage, "Suggested owners"))))


def validate(triage: str, ledger: RunLedger, target: int) -> dict[str, list]:
    """Everything the triage claims that the tools never showed us. Empty lists mean the claim is grounded."""
    return {
        "unknown_labels": [l for l in suggested_labels(triage) if l.lower() not in {x.lower() for x in ledger.repo_labels}],
        "unseen_duplicates": [n for n in cited_duplicates(triage) if n not in ledger.seen_issues or n == target],
        "unseen_owners": [o for o in cited_owners(triage) if o.lower() not in {x.lower() for x in ledger.seen_logins}],
    }
