from web_qa_explorer.ledger import (
    RunLedger,
    append_approval_log,
    findings,
    missing_sections,
    target_from_message,
    unverified_evidence,
    unverified_pages,
)


def _ledger(*evidence, **kw):
    ledger = RunLedger(model="gemini-2.5-flash", target_url="https://shop.example.com/", **kw)
    for text in evidence:
        ledger.record_evidence(text)
    return ledger


REPORT = """## Summary
Tested checkout.

## Findings
### BUG-1 · Cart widget crashes
- **Severity:** high
- **Steps:** open the page
- **Evidence:** `Cart widget failed: TypeError: cart is undefined`

### BUG-2 · Prices API fails
- **Severity:** medium
- **Evidence:** `GET https://shop.example.com/api/prices => 500`

## Coverage
Visited https://shop.example.com/ and https://shop.example.com/cart
"""


def test_budget_blocks_after_limit():
    ledger = _ledger(max_tool_calls=2)
    assert ledger.record_tool_call("browser_click")
    assert ledger.record_tool_call("browser_snapshot")
    assert not ledger.record_tool_call("browser_click")
    assert ledger.calls_by_tool == {"browser_click": 1, "browser_snapshot": 1}


def test_page_budget_allows_revisits_but_not_new_pages():
    ledger = _ledger(max_pages=2)
    assert ledger.record_page("https://a/") and ledger.record_page("https://a/b")
    assert ledger.record_page("https://a/")  # revisit is free
    assert not ledger.record_page("https://a/c")


def test_cost_uses_price_table_and_env_override(monkeypatch):
    ledger = RunLedger(model="gemini-2.5-flash")
    ledger.record_usage(1_000_000, 1_000_000)
    assert ledger.cost_usd() == 0.30 + 2.50
    assert RunLedger(model="unknown").cost_usd() is None
    monkeypatch.setenv("GEMINI_PRICE_IN", "1")
    monkeypatch.setenv("GEMINI_PRICE_OUT", "2")
    assert ledger.cost_usd() == 3.0


def test_target_comes_from_the_message():
    assert target_from_message("https://demo.example.com/app, add two todos") == (
        "https://demo.example.com/app",
        "add two todos",
    )
    assert target_from_message("please test https://x.test") == ("https://x.test", "please test")
    assert target_from_message("hello there") is None


def test_start_resets_everything_for_a_new_run():
    ledger = _ledger(max_tool_calls=1)
    ledger.record_tool_call("browser_click")
    ledger.record_page("https://shop.example.com/")
    ledger.record_evidence("old")
    ledger.record_approval("browser_click(Delete)", True)
    ledger.start("https://other.example.com/", "login")
    assert ledger.tool_calls == 0 and ledger.pages == [] and ledger.evidence == [] and ledger.approvals == []
    assert ledger.target_url == "https://other.example.com/" and ledger.record_tool_call("browser_click")


def test_missing_sections():
    assert missing_sections(REPORT) == []
    assert missing_sections("## Summary\nx") == ["## Findings", "## Coverage"]


def test_findings_stop_at_the_next_section():
    blocks = findings(REPORT)
    assert len(blocks) == 2 and "Coverage" not in blocks[1]


def test_evidence_must_come_from_tool_results():
    ledger = _ledger(
        "Console: Cart widget failed: TypeError: cart is undefined",
        "[GET] https://shop.example.com/api/prices => [500] Internal Server Error",
    )
    assert unverified_evidence(REPORT, ledger) == ["GET https://shop.example.com/api/prices => 500"]  # paraphrased, not copied


def test_evidence_quotes_survive_whitespace_differences():
    ledger = _ledger("Cart widget failed:\n   TypeError: cart is undefined")
    report = REPORT.split("### BUG-2")[0] + "\n## Coverage\nx"
    assert unverified_evidence(report, ledger) == []


def test_a_finding_without_a_quote_is_flagged():
    report = "## Summary\nx\n## Findings\n### BUG-1 · Looks off\n- **Severity:** low\n- **Evidence:** it seemed slow\n## Coverage\nx"
    assert unverified_evidence(report, _ledger("anything")) == ["BUG-1: no quoted evidence"]


def test_no_defects_is_a_valid_report():
    report = "## Summary\nAll good.\n## Findings\nNo defects found.\n## Coverage\nhttps://shop.example.com/"
    assert unverified_evidence(report, _ledger()) == []


def test_pages_on_the_target_host_must_be_in_the_evidence_but_other_hosts_are_ignored():
    ledger = _ledger("- Page URL: https://shop.example.com/")
    report = REPORT + "See https://developer.mozilla.org/docs for background."
    assert unverified_pages(report, ledger) == ["https://shop.example.com/api/prices", "https://shop.example.com/cart"]


def test_approval_log_is_appended_by_code():
    ledger = _ledger()
    assert append_approval_log("## Summary\nx", ledger) == "## Summary\nx"
    ledger.record_approval("browser_click(Delete basket)", False)
    out = append_approval_log("## Summary\nx", ledger)
    assert "## Approval log" in out and "declined: `browser_click(Delete basket)`" in out
