from a2a_duo.ledger import RunLedger, ids_in, ungrounded_librarian_ids, unverified_citations


def test_tool_budget_and_delegation_cap_are_independent():
    ledger = RunLedger(model="m", max_tool_calls=1, max_delegations=1)
    assert ledger.record_tool_call("search_papers") and not ledger.record_tool_call("get_abstract")
    assert ledger.record_delegation() and not ledger.record_delegation()
    assert ledger.calls_by_tool == {"search_papers": 1, "a2a:librarian": 1}


def test_ids_handle_versions_and_old_style():
    assert ids_in("see 2401.12345v2 and cs/0101001 and 9999999") == {"2401.12345", "cs/0101001"}


def test_librarian_ids_must_come_from_mcp_results():
    ledger = RunLedger(model="m")
    ledger.record_mcp_evidence("paper 2401.00001 and 2401.00002")
    ledger.record_librarian_reply("- arXiv:2401.00001 | A | x\n- arXiv:2401.99999 | B | y")
    assert ungrounded_librarian_ids(ledger) == ["2401.99999"]


def test_brief_may_only_cite_ids_the_librarian_sent():
    ledger = RunLedger(model="m")
    ledger.record_librarian_reply("arXiv:2401.00001")
    assert unverified_citations("[a](https://arxiv.org/abs/2401.00001v3)", ledger) == []
    assert unverified_citations("[b](https://arxiv.org/abs/2401.00777)", ledger) == ["2401.00777"]


def test_cost_needs_a_price(monkeypatch):
    monkeypatch.delenv("GEMINI_PRICE_IN", raising=False)
    ledger = RunLedger(model="unknown-model")
    ledger.record_usage(1000, 100)
    assert ledger.cost_usd() is None
    monkeypatch.setenv("GEMINI_PRICE_IN", "1")
    monkeypatch.setenv("GEMINI_PRICE_OUT", "2")
    assert round(ledger.cost_usd(), 6) == 0.0012
