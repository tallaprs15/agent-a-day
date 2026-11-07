import json

from issue_triager.ledger import (
    RunLedger,
    allowed_fetch_url,
    cap_query,
    cited_duplicates,
    cited_owners,
    parse_issue_url,
    search_missing_type,
    slim_response,
    suggested_labels,
    validate,
)

REF = ("octo", "demo", 7)
API = "https://api.github.com"

RAW_ISSUE = {
    "url": API + "/repos/octo/demo/issues/7", "number": 7, "title": "Crash on startup", "state": "open",
    "user": {"login": "alice", "avatar_url": "x"}, "assignees": [{"login": "bob"}], "labels": [{"name": "bug", "color": "d73a4a"}],
    "created_at": "2026-01-01T00:00:00Z", "body": "It crashes.\r\n" + "x" * 2000, "reactions": {"url": "x"},
}
RAW_LABELS = [{"name": "bug", "color": "d73a4a", "description": "Something is broken"}, {"name": "question", "color": "fff"}]


def test_parse_issue_url_accepts_issue_and_pull_links():
    assert parse_issue_url("triage https://github.com/octo/demo/issues/7#issuecomment-1") == REF
    assert parse_issue_url("https://github.com/octo/demo/pull/7/files") == REF
    assert parse_issue_url("https://github.com/octo/demo/discussions/7") is None
    assert parse_issue_url("") is None


def test_fetch_scope_allows_only_this_repos_issue_data():
    ok = [API + "/repos/octo/demo/issues/7", API + "/repos/Octo/Demo/issues/7/comments", API + "/repos/octo/demo/labels?per_page=50",
          API + "/repos/octo/demo/issues?state=all&per_page=10", API + "/search/issues?q=crash+repo:octo/demo+is:issue&per_page=5",
          API + "/search/issues?q=crash%20repo%3Aocto%2Fdemo+is:pr"]
    bad = [API + "/repos/octo/demo/issues/8", API + "/repos/octo/other/issues/7", API + "/repos/octo/demo", API + "/repos/octo/demo/contents/x",
           API + "/user", API + "/search/issues?q=crash", API + "/search/issues?q=crash+repo:octo/other",
           API + "/search/issues?q=crash+repo:octo/demo+repo:evil/x", API + "/search/issues?q=repo:octo/demo+org:evil",
           API + "/search/code?q=repo:octo/demo", "http://api.github.com/repos/octo/demo/issues/7",
           "https://github.com/octo/demo/issues/7", "https://evil.example/repos/octo/demo/issues/7",
           "https://api.github.com@evil.example/repos/octo/demo/issues/7", "file:///etc/passwd", ""]
    assert all(allowed_fetch_url(u, REF) for u in ok), [u for u in ok if not allowed_fetch_url(u, REF)]
    assert not any(allowed_fetch_url(u, REF) for u in bad), [u for u in bad if allowed_fetch_url(u, REF)]


def test_cap_query_limits_page_size_and_depth():
    capped = cap_query(API + "/repos/octo/demo/issues?state=all&per_page=100&page=40")
    assert "per_page=20" in capped and "page=3" in capped and "state=all" in capped
    assert "per_page=20" in cap_query(API + "/repos/octo/demo/issues")
    assert cap_query(API + "/repos/octo/demo/issues/7") == API + "/repos/octo/demo/issues/7"


def test_slim_issue_drops_noise_and_trims_body():
    slim = json.loads(slim_response("Contents of x:\n" + json.dumps(RAW_ISSUE)))
    assert slim[0]["number"] == 7 and slim[0]["labels"] == ["bug"] and slim[0]["author"] == "alice" and slim[0]["assignees"] == ["bob"]
    assert "avatar_url" not in json.dumps(slim) and len(slim[0]["body"]) <= 701 and "\r" not in slim[0]["body"]


def test_slim_handles_labels_search_results_and_non_github_text():
    assert [l["name"] for l in json.loads(slim_response(json.dumps(RAW_LABELS)))] == ["bug", "question"]
    found = json.loads(slim_response(json.dumps({"total_count": 1, "items": [RAW_ISSUE]})))
    assert found[0]["number"] == 7
    assert slim_response("<html>not json</html>") is None and slim_response('{"message": "Not Found"}') is None
    assert slim_response('[{"unknown": 1}]') is None


def test_budget_blocks_after_limit():
    ledger = RunLedger(model="m", max_tool_calls=2)
    assert ledger.record_tool_call("fetch") and ledger.record_tool_call("fetch")
    assert not ledger.record_tool_call("fetch")
    assert ledger.calls_by_tool == {"fetch": 2}


def test_cost_and_unknown_model(monkeypatch):
    monkeypatch.delenv("GEMINI_PRICE_IN", raising=False)
    monkeypatch.delenv("GEMINI_PRICE_OUT", raising=False)
    ledger = RunLedger(model="gemini-3.5-flash-lite")
    ledger.record_usage(1_000_000, 100_000)
    assert round(ledger.cost_usd(), 4) == 0.14
    assert RunLedger(model="new-model").cost_usd() is None
    monkeypatch.setenv("GEMINI_PRICE_IN", "1.0")
    monkeypatch.setenv("GEMINI_PRICE_OUT", "2.0")
    other = RunLedger(model="new-model")
    other.record_usage(1_000_000, 1_000_000)
    assert other.cost_usd() == 3.0


def _ledger_with_evidence():
    ledger = RunLedger(model="m")
    ledger.record_evidence(slim_response(json.dumps(RAW_ISSUE)), target=7)
    other = dict(RAW_ISSUE, number=3, title="Startup crash", user={"login": "carol"}, assignees=[])
    ledger.record_evidence(slim_response(json.dumps({"items": [other]})), target=7)
    ledger.record_evidence(slim_response(json.dumps(RAW_LABELS)), target=7)
    return ledger


def test_evidence_collects_issues_labels_and_people():
    ledger = _ledger_with_evidence()
    assert ledger.target_seen and ledger.labels_listed
    assert ledger.seen_issues == {7, 3} and ledger.repo_labels == {"bug", "question"} and ledger.seen_logins == {"alice", "bob", "carol"}


TRIAGE = """# Triage: octo/demo#7 - Crash on startup
## Classification
bug (high): it crashes.
## Labels
- `bug` - crash
- `urgent` - made up
## Possible duplicates
- #3 - Startup crash - same symptom
- #99 - Ghost - never retrieved
- #7 - itself
## Suggested owners
- @bob - assignee
- @mallory - invented
## Next step
Ask for a stack trace.
"""


def test_validate_flags_everything_not_grounded_in_tool_results():
    assert suggested_labels(TRIAGE) == ["bug", "urgent"]
    assert cited_duplicates(TRIAGE) == [3, 99, 7] and cited_owners(TRIAGE) == ["bob", "mallory"]
    assert validate(TRIAGE, _ledger_with_evidence(), 7) == {
        "unknown_labels": ["urgent"], "unseen_duplicates": [99, 7], "unseen_owners": ["mallory"]}


def test_validate_passes_a_grounded_triage():
    good = TRIAGE.replace("- `urgent` - made up\n", "").replace("- #99 - Ghost - never retrieved\n- #7 - itself\n", "").replace("- @mallory - invented\n", "")
    assert validate(good, _ledger_with_evidence(), 7) == {"unknown_labels": [], "unseen_duplicates": [], "unseen_owners": []}
    assert validate("none of the sections", RunLedger(model="m"), 7) == {"unknown_labels": [], "unseen_duplicates": [], "unseen_owners": []}


def test_search_must_say_is_issue_or_is_pr():
    assert search_missing_type(API + "/search/issues?q=crash+repo:octo/demo")
    assert not search_missing_type(API + "/search/issues?q=crash+repo:octo/demo+is:issue")
    assert not search_missing_type(API + "/search/issues?q=crash%20repo%3Aocto%2Fdemo%20is%3Apr")
    assert not search_missing_type(API + "/repos/octo/demo/issues")
