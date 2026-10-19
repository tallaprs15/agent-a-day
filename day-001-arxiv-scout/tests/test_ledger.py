from arxiv_scout.ledger import RunLedger, unverified_citations


def test_budget_blocks_after_limit():
    ledger = RunLedger(model="gemini-2.5-flash", max_tool_calls=2)
    assert ledger.record_tool_call("search_papers")
    assert ledger.record_tool_call("get_abstract")
    assert not ledger.record_tool_call("get_abstract")
    assert ledger.calls_by_tool == {"search_papers": 1, "get_abstract": 1}


def test_cost_uses_price_table():
    ledger = RunLedger(model="gemini-2.5-flash")
    ledger.record_usage(1_000_000, 100_000)
    assert round(ledger.cost_usd(), 4) == 0.55  # 0.30 + 0.25


def test_unknown_model_has_no_cost(monkeypatch):
    monkeypatch.delenv("GEMINI_PRICE_IN", raising=False)
    monkeypatch.delenv("GEMINI_PRICE_OUT", raising=False)
    assert RunLedger(model="some-new-model").cost_usd() is None


def test_price_env_override(monkeypatch):
    monkeypatch.setenv("GEMINI_PRICE_IN", "1.0")
    monkeypatch.setenv("GEMINI_PRICE_OUT", "2.0")
    ledger = RunLedger(model="some-new-model")
    ledger.record_usage(1_000_000, 1_000_000)
    assert ledger.cost_usd() == 3.0


def test_citations_verified_against_tool_evidence():
    ledger = RunLedger(model="gemini-2.5-flash")
    ledger.record_evidence('{"papers": [{"id": "2401.12345v2"}, {"id": "cs/0101001"}]}')
    brief = (
        "See [arXiv:2401.12345](https://arxiv.org/abs/2401.12345), "
        "[arXiv:cs/0101001](https://arxiv.org/abs/cs/0101001) and 2402.99999."
    )
    assert unverified_citations(brief, ledger) == ["2402.99999"]
