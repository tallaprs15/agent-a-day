import json

from release_notes.demo import HISTORY, make_demo_repo
from release_notes.ledger import (
    MAPPERS,
    RunLedger,
    check_run,
    chunk_commits,
    entries_of,
    parse_log,
    passed,
    render_chunk,
    repo_from_message,
)

SHA_A, SHA_B = "a" * 40, "b" * 40
LOG = (
    "Commit history:\n"
    f"Commit: {SHA_A}\nAuthor: Ann\nDate: 2026-09-02 10:00:00+00:00\nMessage: Fix crash\n\nBody line one.\n\n"
    f"Commit: {SHA_B}\nAuthor: Bo\nDate: 2026-09-01 10:00:00+00:00\nMessage: feat: add csv\n\n"
)


def test_parse_log_reads_commits_and_bodies():
    commits = parse_log(LOG)
    assert [c["short"] for c in commits] == ["aaaaaaa", "bbbbbbb"]
    assert commits[0]["message"] == "Fix crash\n\nBody line one."
    assert commits[1]["author"] == "Bo" and parse_log("Commit history:\n") == []


def test_chunks_are_contiguous_balanced_and_complete():
    commits = [{"sha": str(i), "short": str(i)} for i in range(10)]
    chunks = chunk_commits(commits)
    assert len(chunks) == MAPPERS and [len(c) for c in chunks] == [3, 3, 2, 2]
    assert [c for chunk in chunks for c in chunk] == commits
    assert [len(c) for c in chunk_commits(commits[:2])] == [1, 1, 0, 0]


def test_render_chunk_clips_the_body():
    text = render_chunk(parse_log(LOG), body_chars=5)
    assert text.splitlines()[0] == "aaaaaaa 2026-09-02 Fix crash" and text.splitlines()[1] == "    Body "


def test_entries_of_accepts_dicts_json_and_garbage():
    entries = {"entries": [{"sha": "aaaaaaa"}]}
    assert entries_of(entries) == entries_of(json.dumps(entries)) == [{"sha": "aaaaaaa"}]
    assert entries_of("not json") == [] and entries_of(None) == [] and entries_of({"entries": "x"}) == []


def test_budget_and_cost(monkeypatch):
    monkeypatch.delenv("GEMINI_PRICE_IN", raising=False)
    ledger = RunLedger(model="m", max_tool_calls=1)
    assert ledger.record_tool_call("git_log") and not ledger.record_tool_call("git_log")
    ledger.record_usage(1_000_000, 500_000)
    assert ledger.cost_usd() is None and ledger.cost_usd({"m": (1.0, 2.0)}) == 2.0
    monkeypatch.setenv("GEMINI_PRICE_IN", "0.5")
    monkeypatch.setenv("GEMINI_PRICE_OUT", "1")
    assert ledger.cost_usd() == 1.0
    ledger.start("/r")
    assert ledger.tool_calls == 0 and ledger.model_calls == 0 and ledger.repo == "/r"


def test_add_log_deduplicates():
    ledger = RunLedger(model="m")
    ledger.add_log(LOG)
    assert len(ledger.add_log(LOG)) == 2


def _ledger():
    ledger = RunLedger(model="m")
    ledger.add_log(LOG)
    return ledger


def test_check_run_passes_when_every_commit_is_mapped_and_cited():
    maps = [{"entries": [{"sha": "aaaaaaa"}]}, {"entries": [{"sha": "bbbbbbb"}]}]
    checks = check_run("- Fixed a crash (`aaaaaaa`)\n- CSV export (`bbbbbbb`)", _ledger(), maps)
    assert passed(checks) and checks["commits"] == 2


def test_check_run_catches_gaps_and_invented_shas():
    maps = [{"entries": [{"sha": "aaaaaaa"}, {"sha": "ccccccc"}]}]
    checks = check_run("- Fixed a crash (`aaaaaaa`, `ddddddd`)", _ledger(), maps)
    assert not passed(checks)
    assert checks["unmapped_shas"] == ["bbbbbbb"] and checks["uncited_shas"] == ["bbbbbbb"]
    assert checks["invented_shas"] == ["ccccccc", "ddddddd"]
    assert not check_run("", RunLedger(model="m"), [])["commits_collected"]


def test_repo_comes_from_the_message():
    assert repo_from_message("Release notes for /tmp/app, last 10 commits") == "/tmp/app"
    assert repo_from_message("hello") is None and repo_from_message("what changed lately?") is None


def test_demo_repo_has_the_whole_history():
    import subprocess

    repo = make_demo_repo()
    count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    assert int(count.stdout) == len(HISTORY) == 22
