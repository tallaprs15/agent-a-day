from competitor_scan.ledger import (
    Landscape,
    Product,
    RunLedger,
    normalise,
    response_text,
    render_table,
    split_rows,
    table_problems,
)

SEARCH = (
    "Found 2 results:\n1. Acme Flags\n   URL: https://acme.example/\n   Summary: Acmeisanopensourcefeatureflagplatform.\n"
    "2. Pricing\n   URL: https://acme.example/pricing\n   Summary: x\n"
)
PAGE = (
    "# Acme\n\nAcme is an open-source feature flag platform for teams. Plans start at $20 per seat per month.\n"
    "Short.\n- Self-host it for free under the MIT licence, or use the hosted cloud."
)


def ledger() -> RunLedger:
    led = RunLedger(model="m")
    led.record_search(SEARCH)
    led.record_page("https://acme.example/pricing", PAGE)
    return led


def product(**kw) -> Product:
    base = dict(
        name="Acme", url="https://acme.example/pricing", what_it_is="Open-source flags.", pricing_model="Paid",
        starting_price="$20/seat/mo", open_source="Yes", differentiator="Self-hostable",
        what_it_is_passage="P1", pricing_passage="P2",
    )
    return Product(**{**base, **kw})


def test_normalise_ignores_spacing_and_punctuation():
    assert normalise("Open-source  feature\\nflag") == normalise("opensource featureflag")


def test_page_is_split_into_numbered_passages_in_order():
    led = ledger()
    assert led.passage("P1", "https://acme.example/pricing").startswith("Acme is an open-source")
    assert led.passage("p2", "https://acme.example/pricing/").startswith("Plans start at $20")
    assert "Short." not in [t for _, t in led.passages.values()]  # too short to cite
    assert len(led.passages) == 3


def test_passage_ids_are_bound_to_their_page():
    led = ledger()
    led.record_page("https://other.example/", "Other is a different product with its own long sentence.")
    assert led.passage("P4", "https://other.example/")
    assert led.passage("P4", "https://acme.example/pricing") is None
    assert led.passage("P99", "https://acme.example/pricing") is None


def test_response_text_unwraps_mcp_shapes():
    assert "hello" in response_text({"content": [{"type": "text", "text": "hello"}], "isError": False})
    assert response_text(["a", {"result": "b"}]) == "a\nb"


def test_valid_row_is_kept():
    kept, dropped = split_rows(Landscape(market="m", products=[product()]), ledger())
    assert len(kept) == 1 and not dropped


def test_missing_description_passage_drops_the_row():
    for pid in ("P99", ""):
        kept, dropped = split_rows(Landscape(market="m", products=[product(what_it_is_passage=pid)]), ledger())
        assert not kept and "does not exist" in dropped[0]


def test_row_url_comes_from_the_cited_passage_not_the_model():
    row = product(url="https://made-up.example/")
    kept, _ = split_rows(Landscape(market="m", products=[row]), ledger())
    assert kept[0].url == "https://acme.example/pricing"


def test_pricing_passage_from_another_page_is_not_accepted():
    led = ledger()
    led.record_page("https://other.example/", "Other plans start at $20 per seat per month, billed yearly.")
    kept, dropped = split_rows(Landscape(market="m", products=[product(pricing_passage="P4")]), led)
    assert kept[0].pricing_model == "Unknown" and "pricing not backed" in dropped[0]


def test_invented_passage_id_resets_pricing_but_keeps_the_row():
    kept, dropped = split_rows(Landscape(market="m", products=[product(pricing_passage="P99")]), ledger())
    assert len(kept) == 1 and "pricing not backed" in dropped[0]
    assert (kept[0].pricing_model, kept[0].starting_price, kept[0].pricing_passage) == ("Unknown", "Unknown", "")


def test_price_not_in_cited_passage_is_reset():
    kept, dropped = split_rows(Landscape(market="m", products=[product(starting_price="$5/seat/mo")]), ledger())
    assert kept[0].pricing_model == "Unknown" and "pricing not backed" in dropped[0]


def test_free_label_needs_the_word_free_in_the_cited_passage():
    kept, dropped = split_rows(Landscape(market="m", products=[product(pricing_model="Freemium", starting_price="Free")]), ledger())
    assert kept[0].pricing_model == "Unknown" and dropped  # P2 says "Plans start at $20", nothing about free
    led = ledger()
    led.record_page("https://acme.example/pricing", "Self-host it for free under the MIT licence, or use the cloud.")
    pid = next(i for i, (_, t) in led.passages.items() if t.startswith("Self-host it for free") and i != "P3")
    kept, dropped = split_rows(Landscape(market="m", products=[product(pricing_model="Freemium", starting_price="Free", pricing_passage=pid)]), led)
    assert kept[0].pricing_model == "Freemium" and not dropped


def test_price_in_cited_passage_survives():
    kept, dropped = split_rows(Landscape(market="m", products=[product()]), ledger())
    assert kept[0].starting_price == "$20/seat/mo" and not dropped


def test_unknown_pricing_needs_no_pricing_passage():
    row = product(pricing_model="Unknown", starting_price="Unknown", pricing_passage="")
    kept, _ = split_rows(Landscape(market="m", products=[row]), ledger())
    assert len(kept) == 1


def test_duplicates_are_dropped():
    kept, dropped = split_rows(Landscape(market="m", products=[product(), product(name="ACME")]), ledger())
    assert len(kept) == 1 and "duplicate" in dropped[0]


def test_table_renders_pipes_safely_and_lists_dropped_rows():
    led = ledger()
    land = Landscape(market="flags", products=[product(differentiator="a | b")], caveats=["pricing page of X unreachable"])
    kept, dropped = split_rows(land, led)
    text = render_table(land, kept, dropped + ["Ghost: duplicate row"], led)
    assert "a / b" in text and 'Acme is an open-source feature flag platform for teams.' in text and "(P1)" in text and "pricing page of X unreachable" in text and "Not verified" in text


def test_table_problems_needs_enough_rows_and_known_urls():
    led = ledger()
    one = render_table(Landscape(market="m", products=[product()]), [product()], [], led)
    assert any("need at least 2" in p for p in table_problems(one, led))
    assert any("not returned by any tool" in p for p in table_problems("| [X](https://nope.example/) |", led))


def test_may_fetch_allows_search_hits_and_same_host_only():
    led = ledger()
    assert led.may_fetch("https://acme.example/pricing")
    assert led.may_fetch("https://acme.example/docs")
    assert not led.may_fetch("http://169.254.169.254/latest")


def test_search_budget_is_separate_from_total_budget():
    led = RunLedger(model="m", max_tool_calls=10, max_searches=2)
    assert led.record_tool_call("search") is None and led.record_tool_call("search") is None
    assert "Search budget" in led.record_tool_call("search")
    assert led.record_tool_call("fetch") is None


def test_reset_clears_evidence_but_keeps_limits():
    led = ledger()
    led.record_tool_call("search")
    led.reset()
    assert led.tool_calls == 0 and not led.seen_urls and not led.passages
    assert led.max_tool_calls == 14


def test_already_requested_urls_are_tracked_across_spelling():
    led = ledger()
    led.requested_urls.add("https://acme.example/pricing")
    assert led.already_requested("https://acme.example/pricing/#plans")
    assert not led.already_requested("https://acme.example/docs")


def test_vendor_page_filter():
    assert RunLedger.is_vendor_page("https://acme.example/pricing")
    assert RunLedger.is_vendor_page("https://acme.example/")
    for url in ("https://acme.example/blog/x", "https://acme.example/compare/other", "https://github.com/acme/acme",
                "https://posthog.com/best-open-source-flags", "https://acme.example/post/launchdarkly-vs-acme"):
        assert not RunLedger.is_vendor_page(url), url


def test_urls_inside_quoted_passages_are_not_checked_as_links():
    led = ledger()
    row = product(what_it_is_passage="P1")
    report = render_table(Landscape(market="m", products=[row]), [row], [], led)
    report += '- **X**: "see [docs](https://elsewhere.example/page)"\n'
    assert not any("elsewhere" in p for p in table_problems(report, led))
