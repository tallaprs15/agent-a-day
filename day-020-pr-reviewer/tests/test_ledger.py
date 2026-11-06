from pr_reviewer.ledger import RunLedger, allowed_fetch_url, parse_pr_url, unseen_files

PR = ("octo", "demo", 7)
DIFF = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x\n+y\n"


def test_parse_pr_url():
    assert parse_pr_url("please review https://github.com/octo/demo/pull/7/files now") == PR
    assert parse_pr_url("https://github.com/octo/demo/issues/7") is None
    assert parse_pr_url("") is None


def test_fetch_scope_allows_only_this_pr():
    ok = ["https://github.com/octo/demo/pull/7", "https://github.com/octo/demo/pull/7.diff",
          "https://github.com/Octo/Demo/pull/7.patch", "https://github.com/octo/demo/pull/7/files"]
    bad = ["https://github.com/octo/demo/pull/8.diff", "https://github.com/octo/other/pull/7.diff",
           "https://evil.example/octo/demo/pull/7.diff", "http://github.com/octo/demo/pull/7.diff",
           "https://github.com/octo/demo/pull/7.diff/../../x", "https://api.github.com/user", "file:///etc/passwd", ""]
    assert all(allowed_fetch_url(u, PR) for u in ok)
    assert not any(allowed_fetch_url(u, PR) for u in bad)


def test_budget_blocks_after_limit():
    ledger = RunLedger(model="gemini-2.5-flash", max_tool_calls=2)
    assert ledger.record_tool_call("fetch") and ledger.record_tool_call("fetch")
    assert not ledger.record_tool_call("fetch")
    assert ledger.calls_by_tool == {"fetch": 2}


def test_cost_and_unknown_model(monkeypatch):
    monkeypatch.delenv("GEMINI_PRICE_IN", raising=False)
    monkeypatch.delenv("GEMINI_PRICE_OUT", raising=False)
    ledger = RunLedger(model="gemini-2.5-flash")
    ledger.record_usage(1_000_000, 100_000)
    assert round(ledger.cost_usd(), 4) == 0.55
    assert RunLedger(model="new-model").cost_usd() is None
    monkeypatch.setenv("GEMINI_PRICE_IN", "1.0")
    monkeypatch.setenv("GEMINI_PRICE_OUT", "2.0")
    other = RunLedger(model="new-model")
    other.record_usage(1_000_000, 1_000_000)
    assert other.cost_usd() == 3.0


def test_files_in_review_must_appear_in_the_diff():
    ledger = RunLedger(model="m")
    ledger.record_evidence(DIFF)
    assert ledger.seen_files == {"src/app.py"}
    review = "| high | `src/app.py:42` | bug |\n| low | `app.py` | ok (suffix) |\n| low | `lib/ghost.py:3` | invented |\nUse `os.path` and `foo()`."
    assert unseen_files(review, ledger) == ["lib/ghost.py"]


def test_evidence_survives_json_escaped_newlines():
    ledger = RunLedger(model="m")
    ledger.record_evidence(DIFF.replace("\n", "\\n"))
    assert "src/app.py" in ledger.seen_files
