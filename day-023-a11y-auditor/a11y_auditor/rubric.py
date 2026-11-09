"""The audit rubric and the scope rules, as plain data and code (not prompt text).

The rubric lists the WCAG 2.2 success criteria that can be judged from a page's accessibility
snapshot. The agent must grade every one, and the report checks hold it to that list.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Criterion:
    id: str
    name: str
    level: str  # WCAG conformance level: "A" or "AA"
    look_for: str  # what in the snapshot shows a failure


RUBRIC: tuple[Criterion, ...] = (
    Criterion("1.1.1", "Non-text Content", "A", "an img, or a link/button holding only an image, with no accessible name (a bare `link` or `img` line)"),
    Criterion("1.3.1", "Info and Relationships", "A", "skipped heading levels, no h1, or tables/lists that are not real structure"),
    Criterion("2.1.1", "Keyboard", "A", "after Tab, focus never reaches an interactive control, or lands on something that is not one"),
    Criterion("2.4.1", "Bypass Blocks", "A", "no skip link and no landmarks (main, nav) to jump past repeated content"),
    Criterion("2.4.2", "Page Titled", "A", "a missing, empty or generic page title (no title shown for the page)"),
    Criterion("2.4.4", "Link Purpose (In Context)", "A", "links named 'click here', 'read more', 'here' or empty"),
    Criterion("2.4.6", "Headings and Labels", "AA", "empty or non-descriptive headings and labels"),
    Criterion("3.3.2", "Labels or Instructions", "A", "a textbox, combobox or checkbox with no accessible name"),
    Criterion("4.1.2", "Name, Role, Value", "A", "a button or custom control with no accessible name"),
)

BY_ID = {c.id: c for c in RUBRIC}
STATUSES = ("Pass", "Fail", "Not assessed")
SEVERITIES = ("critical", "serious", "moderate", "minor")


def rubric_table() -> str:
    """The rubric as Markdown, for the agent's instruction."""
    return "\n".join(f"- {c.id} {c.name} (Level {c.level}): fails when you see {c.look_for}" for c in RUBRIC)


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def in_scope(url: str, target_url: str) -> bool:
    """http(s) only, and only the target's own host or its subdomains."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host, target = host_of(url), host_of(target_url)
    return bool(host) and bool(target) and (host == target or host.endswith("." + target))
