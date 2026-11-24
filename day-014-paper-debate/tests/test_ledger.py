from paper_debate.ledger import RunLedger, claim_ids, debate_checks, unverified_citations

BRIEF = "Title: X\narXiv: 1706.03762\nClaims:\nC1: Beats BLEU baselines.\nC2: Trains faster.\n"
TRANSCRIPT = (
    "### Round 1 · Advocate\nC1 holds.\n"
    "### Round 1 · Skeptic\nSee [arXiv:2401.12345](https://arxiv.org/abs/2401.12345).\n"
)


def _ledger():
    ledger = RunLedger(model="gemini-2.5-flash")
    ledger.record_evidence('{"papers": [{"id": "2401.12345v2"}, {"id": "1706.03762"}]}')
    return ledger


def _state(**over):
    ruling = {
        "claims": [
            {"claim_id": "C1", "verdict": "holds", "rationale": "ok"},
            {"claim_id": "C2", "verdict": "partly", "rationale": "caveat"},
        ],
        "summary": "s",
        "confidence": "low",
    }
    return {"brief": BRIEF, "transcript": TRANSCRIPT, "ruling": ruling, **over}


def test_budget_blocks_after_limit():
    ledger = RunLedger(model="m", max_tool_calls=2)
    assert ledger.record_tool_call("search_papers") is None
    assert ledger.record_tool_call("get_abstract") is None
    assert "budget" in ledger.record_tool_call("get_abstract")
    assert ledger.calls_by_tool == {"search_papers": 1, "get_abstract": 1}


def test_cost_uses_price_table(monkeypatch):
    monkeypatch.delenv("GEMINI_PRICE_IN", raising=False)
    ledger = RunLedger(model="gemini-2.5-flash")
    ledger.record_usage(1_000_000, 100_000)
    assert round(ledger.cost_usd(), 4) == 0.55


def test_unknown_model_has_no_cost(monkeypatch):
    monkeypatch.delenv("GEMINI_PRICE_IN", raising=False)
    monkeypatch.delenv("GEMINI_PRICE_OUT", raising=False)
    assert RunLedger(model="some-new-model").cost_usd() is None


def test_citations_verified_against_tool_evidence():
    text = "[arXiv:2401.12345v1] and arXiv:2402.99999 and 12.5 BLEU in 2017.1234"
    assert "2402.99999" in unverified_citations(text, _ledger())
    assert "2401.12345" not in unverified_citations(text, _ledger())


def test_claim_ids_in_order():
    assert claim_ids(BRIEF) == ["C1", "C2"]
    assert claim_ids("C1: a\nC1: dup\n**C2:** b") == ["C1", "C2"]


def test_all_checks_pass_on_a_good_run():
    assert all(debate_checks(_state(), _ledger(), rounds_expected=1).values())


def test_missing_skeptic_turn_fails_both_sides_heard():
    state = _state(transcript="### Round 1 · Advocate\nhi\n")
    assert not debate_checks(state, _ledger(), 1)["both_sides_heard"]


def test_ruling_must_cover_every_claim():
    state = _state()
    state["ruling"]["claims"].pop()
    assert not debate_checks(state, _ledger(), 1)["ruling_covers_every_claim"]


def test_invented_citation_in_ruling_fails():
    state = _state()
    state["ruling"]["summary"] = "Confirmed by arXiv:2402.99999."
    assert not debate_checks(state, _ledger(), 1)["citations_verified"]


def test_ruling_as_json_text_is_accepted():
    import json

    state = _state()
    state["ruling"] = json.dumps(state["ruling"])
    assert all(debate_checks(state, _ledger(), 1).values())
