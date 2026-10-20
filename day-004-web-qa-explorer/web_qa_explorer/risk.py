"""Which browser actions need a human's approval, and which pages the agent may visit.

Both rules are plain code, not prompt text, so a model cannot talk its way past them.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

# Labels of controls that change state or spend something: the agent must ask first.
RISKY_LABEL = re.compile(
    r"\b(delete|remove|clear|destroy|erase|reset|deactivate|close account|cancel|"
    r"pay|buy|purchase|order|checkout|donate|transfer|subscribe|unsubscribe|"
    r"send|post|publish|submit|confirm|sign ?out|log ?out)\b",
    re.I,
)
# Typing into these fields is never done without approval, whatever the text.
SENSITIVE_FIELD = re.compile(r"\b(password|passcode|card|cvv|cvc|iban|ssn|secret|token|api key)\b", re.I)

# Tools that act on the page (the rest only observe it).
ACTING_TOOLS = {"browser_click", "browser_type"}


def label_of(args: dict[str, Any]) -> str:
    """The human-readable description the model gave for the element, plus its ref."""
    return f"{args.get('element') or ''} {args.get('target') or args.get('ref') or ''}".strip()


def is_risky(args: dict[str, Any]) -> bool:
    """True when an acting call touches a state-changing or sensitive control.

    ADK calls this with the tool's arguments only, so a typing call is recognised by its
    `text` argument. Observing tools never reach here with an `element` label.
    """
    # Quoted spans are page content (a todo called 'Buy milk'), not the control's own name.
    label = re.sub(r"""(['"]).*?\1""", " ", label_of(args))
    if "text" in args:
        return bool(SENSITIVE_FIELD.search(label) or RISKY_LABEL.search(label))
    return bool(RISKY_LABEL.search(label))


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def in_scope(url: str, target_url: str) -> bool:
    """http(s) only, and only the target's own host or its subdomains.

    This also keeps the browser away from localhost and cloud metadata addresses unless
    the target itself is one of them.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host, target = host_of(url), host_of(target_url)
    return bool(host) and bool(target) and (host == target or host.endswith("." + target))
