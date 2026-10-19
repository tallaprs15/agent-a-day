"""Regenerate the progress table in the root README from each day's meta.json.

Run by CI on every push to main, so daily PRs never touch the root README
and never conflict with each other.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
START, END = "<!-- index:start -->", "<!-- index:end -->"


def rows() -> list[str]:
    out = []
    for meta_path in sorted(ROOT.glob("day-*/meta.json")):
        m = json.loads(meta_path.read_text())
        folder = meta_path.parent.name
        servers = " · ".join(f"`{s}`" for s in m["mcp_servers"])
        out.append(
            f"| {m['day']:03d} | [{m['title']}]({folder}/) | {m['summary']} | {m['pattern']} | {servers} |"
        )
    return out


def main() -> None:
    body = rows()
    table = "\n".join(
        [
            f"**Agents built so far: {len(body)}**",
            "",
            "| Day | Agent | What it does | Pattern | MCP servers |",
            "|---:|---|---|---|---|",
            *body,
        ]
    )
    text = README.read_text()
    new = re.sub(
        re.escape(START) + r".*?" + re.escape(END),
        f"{START}\n{table}\n{END}",
        text,
        flags=re.S,
    )
    README.write_text(new)


if __name__ == "__main__":
    main()
