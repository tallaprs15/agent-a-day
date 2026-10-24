from aws_architect.ledger import RunLedger, clean_url, missing_sections, unverified_citations

DOC = "https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingEncryption.html"


def test_total_budget_is_enforced():
    ledger = RunLedger(model="m", max_tool_calls=2, max_calls_per_reviewer=5)
    assert ledger.record_tool_call("search_documentation", "a") is None
    assert ledger.record_tool_call("read_sections", "b") is None
    assert "Total tool budget" in ledger.record_tool_call("search_documentation", "c")
    assert ledger.tool_calls == 2 and ledger.calls_by_tool == {"search_documentation": 1, "read_sections": 1}


def test_per_reviewer_budget_stops_one_branch_starving_the_others():
    ledger = RunLedger(model="m", max_tool_calls=10, max_calls_per_reviewer=2)
    assert ledger.record_tool_call("search_documentation", "cost") is None
    assert ledger.record_tool_call("search_documentation", "cost") is None
    assert "Your budget" in ledger.record_tool_call("search_documentation", "cost")
    assert ledger.record_tool_call("search_documentation", "security") is None  # other branch unaffected
    assert ledger.calls_by_agent == {"cost": 2, "security": 1}


def test_usage_and_cost():
    ledger = RunLedger(model="gemini-2.5-flash")
    ledger.record_usage(1_000_000, 100_000)
    ledger.record_usage(None, None)
    assert ledger.model_calls == 2
    assert abs(ledger.cost_usd() - 0.55) < 1e-9
    assert RunLedger(model="unknown").cost_usd() is None


def test_clean_url_ignores_fragment_and_punctuation():
    assert clean_url(DOC + "#section.") == DOC


def test_citation_check_flags_links_no_tool_returned():
    ledger = RunLedger(model="m")
    ledger.record_evidence('{"url": "https:\\/\\/docs.aws.amazon.com\\/AmazonS3\\/latest\\/userguide\\/UsingEncryption.html"}')
    report = f"See [S3]({DOC}#x) and [made up](https://docs.aws.amazon.com/fake/page.html) and [blog](https://example.com/a)."
    assert unverified_citations(report, ledger) == ["https://docs.aws.amazon.com/fake/page.html"]


def test_missing_sections():
    assert missing_sections("# Architecture review\n## Summary\n") != []
    full = "\n".join(f"{h}\n" for h in ["## Summary", "## Top priorities", "## Cost optimization", "## Security", "## Reliability", "## Trade-offs", "## Sources"])
    assert missing_sections(full) == []
