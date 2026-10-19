import pytest

from docs_copilot.ledger import (
    RunLedger,
    called_names,
    is_unanswered,
    missing_sections,
    question_from_message,
    ungrounded_calls,
    unverified_sources,
)


def _ledger(*evidence, **kw):
    ledger = RunLedger(model="gemini-2.5-flash", **kw)
    for text in evidence:
        ledger.record_evidence(text)
    return ledger


def test_budget_blocks_after_limit():
    ledger = RunLedger(model="gemini-2.5-flash", max_tool_calls=2)
    assert ledger.record_tool_call("query-docs")
    assert ledger.record_tool_call("resolve-library-id")
    assert not ledger.record_tool_call("query-docs")
    assert ledger.calls_by_tool == {"query-docs": 1, "resolve-library-id": 1}


def test_cost_uses_price_table():
    ledger = RunLedger(model="gemini-2.5-flash")
    ledger.record_usage(1_000_000, 100_000)
    assert round(ledger.cost_usd(), 4) == 0.55  # 0.30 + 0.25


def test_unknown_model_has_no_cost(monkeypatch):
    monkeypatch.delenv("GEMINI_PRICE_IN", raising=False)
    monkeypatch.delenv("GEMINI_PRICE_OUT", raising=False)
    assert RunLedger(model="some-new-model").cost_usd() is None


@pytest.mark.parametrize("text", ["", "hi", "hello there", "  ok  "])
def test_greetings_are_not_questions(text):
    assert question_from_message(text) is None


def test_real_question_is_kept():
    assert question_from_message(" How do I declare a dependency in FastAPI? ") == "How do I declare a dependency in FastAPI?"


def test_missing_sections():
    assert missing_sections("## Answer\nx\n## Sources\n- y") == []
    assert missing_sections("## Answer\nx") == ["## Sources"]


def test_sources_must_have_been_returned_by_a_tool():
    ledger = _ledger("Context7-compatible library ID: /websites/fastapi_tiangolo", "see https://fastapi.tiangolo.com/tutorial/dependencies/")
    good = "## Sources\n- /websites/fastapi_tiangolo\n- https://fastapi.tiangolo.com/tutorial/dependencies/"
    assert unverified_sources(good, ledger) == []
    bad = good + "\n- https://fastapi.tiangolo.com/made-up/\n- /websites/not_real"
    assert unverified_sources(bad, ledger) == ["/websites/not_real", "https://fastapi.tiangolo.com/made-up"]


def test_urls_outside_the_sources_section_are_not_judged():
    assert unverified_sources("## Answer\nSee https://example.com/x\n## Sources\n- none", _ledger("nothing")) == []


def test_called_names_ignores_builtins_and_names_the_snippet_defines():
    code = "def get_user(db):\n    return len(db.fetch_all())\nresult = get_user(Depends(get_db))\nprint(result)\n"
    assert called_names(code) == {"fetch_all", "Depends"}


def test_ungrounded_calls_flags_methods_missing_from_the_docs():
    ledger = _ledger("use Depends(get_db) inside the path operation; app.include_router(router)")
    answer = "## Answer\n```python\nuser = Depends(get_db)\napp.include_router(router)\napp.magic_inject(router)\n```\n## Sources\n"
    assert ungrounded_calls(answer, ledger) == ["magic_inject"]
    assert ungrounded_calls("## Answer\nno code here\n## Sources\n", ledger) == []


def test_declined_answer_is_detected():
    assert is_unanswered("## Answer\nThe retrieved docs do not cover this\n## Sources\n- none")
    assert not is_unanswered("## Answer\nUse Depends.\n## Sources\n- /x/y")


def test_fragment_of_a_returned_page_is_accepted():
    ledger = _ledger("https://learn.microsoft.com/azure/storage/blobs/quickstart?pivots=python")
    assert unverified_sources("## Sources\n- https://learn.microsoft.com/azure/storage/blobs/quickstart?pivots=python#code", ledger) == []
