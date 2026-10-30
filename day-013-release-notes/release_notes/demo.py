"""A small, invented project history (22 commits) so the agent can be tried without a repository of your own."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

# (date, subject, body), oldest first. Mixed styles on purpose: conventional prefixes, plain English, a revert.
HISTORY = [
    ("2026-09-01", "feat: add csv export for reports", ""),
    ("2026-09-02", "Fix crash when the config file is missing", "Fall back to defaults and print a warning instead of a traceback."),
    ("2026-09-03", "docs: describe the --format flag", ""),
    ("2026-09-04", "bump pytest to 8.4", ""),
    ("2026-09-07", "perf: cache parsed templates between runs", "Cuts a 40-report batch from 9s to 3s on the reference laptop."),
    ("2026-09-08", "feat: add --since filter to the list command", ""),
    ("2026-09-09", "refactor: split the renderer into smaller functions", ""),
    ("2026-09-10", "Fix off-by-one in pagination", "The last page repeated the final row."),
    ("2026-09-11", "ci: run tests on Python 3.12", ""),
    ("2026-09-14", "feat!: rename --out to --output", "BREAKING CHANGE: scripts passing --out must switch to --output."),
    ("2026-09-15", "Add dark theme to the HTML report", ""),
    ("2026-09-16", "fix: escape HTML in report titles", "Titles containing angle brackets broke the page."),
    ("2026-09-17", "chore: remove unused imports", ""),
    ("2026-09-18", "docs: add a migration note for --output", ""),
    ("2026-09-21", "Revert \"Add dark theme to the HTML report\"", "The theme broke printing; will return with a print stylesheet."),
    ("2026-09-22", "perf: stream large inputs instead of loading them whole", "Memory use is now flat for inputs over 1 GB."),
    ("2026-09-23", "feat: add a --json output mode", ""),
    ("2026-09-24", "test: cover the pagination edge cases", ""),
    ("2026-09-25", "Fix wrong timezone in generated timestamps", "Reports now use UTC consistently."),
    ("2026-09-28", "Drop support for Python 3.9", "BREAKING: the minimum supported version is now 3.10."),
    ("2026-09-29", "docs: fix typos in the README", ""),
    ("2026-09-30", "update lockfile", ""),
]


def make_demo_repo() -> Path:
    """Create the history above in a throwaway git repository and return its path."""
    repo = Path(tempfile.mkdtemp(prefix="release-notes-demo-")) / "reportkit"
    repo.mkdir()
    base = ["git", "-c", "user.name=Demo Dev", "-c", "user.email=dev@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run([*base, "init", "-q"], cwd=repo, check=True)
    for i, (day, subject, body) in enumerate(HISTORY, start=1):
        (repo / "CHANGES.txt").write_text(f"change {i}\n")
        subprocess.run([*base, "add", "."], cwd=repo, check=True)
        message = subject + (f"\n\n{body}" if body else "")
        env = {**os.environ, "GIT_AUTHOR_DATE": f"{day}T12:00:00+00:00", "GIT_COMMITTER_DATE": f"{day}T12:00:00+00:00"}
        subprocess.run([*base, "commit", "-q", "-m", message], cwd=repo, check=True, env=env)
    return repo
