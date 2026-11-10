from dataset_scout.ledger import RunLedger, cited_datasets, truncate_response, unverified_datasets

SEARCH = """### acme/support-tickets

**Author:** acme | **Downloads:** 5K | **Likes:** 2

**Link:** [https://hf.co/datasets/acme/support-tickets](https://hf.co/datasets/acme/support-tickets)
"""
DETAILS = "# Other-Org/Chat_v2.1\n\n## Overview\n- **Author:** other\n"


def _ledger(**kw):
    return RunLedger(model="gemini-2.5-flash", **kw)


def test_budget_blocks_after_limit_and_counts_structure_calls():
    ledger = _ledger(max_tool_calls=2)
    assert ledger.record_tool_call("hub_repo_search", {"query": "x"})
    assert ledger.record_tool_call("hub_repo_details", {"operations": ["overview", "dataset_structure"]})
    assert not ledger.record_tool_call("hub_repo_details", {})
    assert ledger.calls_by_tool == {"hub_repo_search": 1, "hub_repo_details": 1}
    assert ledger.structure_calls == 1


def test_cost_uses_price_table_and_env_override(monkeypatch):
    monkeypatch.delenv("GEMINI_PRICE_IN", raising=False)
    monkeypatch.delenv("GEMINI_PRICE_OUT", raising=False)
    ledger = _ledger()
    ledger.record_usage(1_000_000, 100_000)
    assert round(ledger.cost_usd(), 4) == 0.55
    assert RunLedger(model="some-new-model").cost_usd() is None
    monkeypatch.setenv("GEMINI_PRICE_IN", "1.0")
    monkeypatch.setenv("GEMINI_PRICE_OUT", "2.0")
    assert RunLedger(model="some-new-model").cost_usd() == 0.0


def test_evidence_collects_ids_from_headings_and_links():
    ledger = _ledger()
    ledger.record_evidence(SEARCH)
    ledger.record_evidence(DETAILS)
    assert ledger.seen_datasets == {"acme/support-tickets", "other-org/chat_v2.1"}


def test_citations_are_verified_against_tool_evidence():
    ledger = _ledger()
    ledger.record_evidence(SEARCH)
    guide = (
        "[acme/support-tickets](https://huggingface.co/datasets/acme/support-tickets) and "
        "[ghost/data](https://huggingface.co/datasets/ghost/data).\n"
        '```python\nload_dataset("acme/support-tickets")\nload_dataset("fake/thing")\n```'
    )
    assert cited_datasets(guide) == {"acme/support-tickets", "ghost/data", "fake/thing"}
    assert unverified_datasets(guide, ledger) == ["fake/thing", "ghost/data"]


def test_truncate_response_keeps_shape():
    assert truncate_response("a" * 50, 10).endswith("[truncated]")
    resp = {"content": [{"type": "text", "text": "a" * 50}, {"type": "text", "text": "b" * 50}]}
    out = truncate_response(resp, 60)
    assert out["content"][0]["text"] == "a" * 50 and out["content"][1]["text"].startswith("b" * 10)
    assert out["content"][1]["text"].endswith("[truncated]")
    assert truncate_response(resp, 1000) == resp
