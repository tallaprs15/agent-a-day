from cf_docs_helper.ledger import RunLedger, cited_links, doc_path, missing_sections, unverified_links


def test_budget_is_enforced_and_counted_per_tool():
    ledger = RunLedger(model="m", max_tool_calls=2)
    assert ledger.record_tool_call("a") and ledger.record_tool_call("a")
    assert not ledger.record_tool_call("a")
    assert ledger.tool_calls == 2 and ledger.calls_by_tool == {"a": 2}


def test_cost_needs_a_known_price(monkeypatch):
    monkeypatch.delenv("GEMINI_PRICE_IN", raising=False)
    monkeypatch.delenv("GEMINI_PRICE_OUT", raising=False)
    ledger = RunLedger(model="unknown-model")
    ledger.record_usage(1_000_000, 1_000_000)
    assert ledger.cost_usd() is None
    monkeypatch.setenv("GEMINI_PRICE_IN", "0.5")
    monkeypatch.setenv("GEMINI_PRICE_OUT", "2")
    assert ledger.cost_usd() == 2.5
    assert ledger.model_calls == 1


def test_doc_path_normalises():
    assert doc_path("https://developers.cloudflare.com/kv/platform/limits/") == "/kv/platform/limits"
    assert doc_path("https://developers.cloudflare.com/kv/platform/limits/#writes.") == "/kv/platform/limits"
    assert doc_path("/kv/") == "/kv"


EVIDENCE = (
    "<url>https://developers.cloudflare.com/changelog/post/2025-01-27-kv/</url> "
    "Consult the [limits](/kv/platform/limits/) and [Workers plans](/workers/platform/pricing/)."
)


def test_links_from_results_and_inside_results_are_accepted():
    ledger = RunLedger(model="m")
    ledger.record_evidence(EVIDENCE)
    answer = (
        "## Answer\nx\n## Sources\n"
        "- https://developers.cloudflare.com/kv/platform/limits/\n"
        "- [pricing](https://developers.cloudflare.com/workers/platform/pricing)\n"
        "- https://developers.cloudflare.com/changelog/post/2025-01-27-kv/#top\n"
    )
    assert unverified_links(answer, ledger) == []


def test_invented_link_is_flagged():
    ledger = RunLedger(model="m")
    ledger.record_evidence(EVIDENCE)
    answer = "See [x](https://developers.cloudflare.com/kv/made-up-page/) and https://developers.cloudflare.com/kv/platform/limits/."
    assert unverified_links(answer, ledger) == ["https://developers.cloudflare.com/kv/made-up-page/"]


def test_links_to_other_sites_are_not_cited_docs():
    assert cited_links("see https://example.com/a and https://developers.cloudflare.com/r2/.") == [
        "https://developers.cloudflare.com/r2/"
    ]


def test_missing_sections():
    assert missing_sections("## Answer\nx") == ["## Sources"]
    assert missing_sections("## Answer\n## Sources") == []
