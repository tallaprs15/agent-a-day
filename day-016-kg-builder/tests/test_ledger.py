from kg_builder.ledger import (
    MAX_ENTITIES,
    RunLedger,
    normalise_relation,
    render_report,
    response_json,
    squash,
    urls_from_message,
)

URL_A = "https://example.com/a"
URL_B = "https://example.com/b"
PAGE_A = (
    "Contents of https://example.com/a:\n"
    "Ada Lovelace worked with [Charles Babbage](https://example.com/cb) on the Analytical Engine.\n"
    "The Analytical Engine was designed by Charles Babbage in London."
)
PAGE_B = "Charles Babbage designed the Difference Engine before the Analytical Engine."


def _ledger():
    ledger = RunLedger(model="gemini-2.5-flash")
    ledger.urls = [URL_A, URL_B]
    ledger.pages = {URL_A: PAGE_A, URL_B: PAGE_B}
    ledger.fetched = {URL_A, URL_B}
    return ledger


def _entity(name, quote, source=URL_A, kind="person"):
    return {"name": name, "type": kind, "description": f"About {name}.", "quote": quote, "source": source}


def _relation(src, dst, quote, rel="worked_with", source=URL_A):
    return {"source_entity": src, "target_entity": dst, "relation": rel, "quote": quote, "source": source}


GOOD_QUOTE = "Ada Lovelace worked with Charles Babbage on the Analytical Engine."


def test_urls_from_message_keeps_https_dedupes_and_caps():
    text = "see https://a.com/x, https://a.com/x http://insecure.com/y (https://b.com/z). https://c.com https://d.com"
    assert urls_from_message(text) == ["https://a.com/x", "https://b.com/z", "https://c.com"]
    assert urls_from_message("no links here") == []


def test_squash_ignores_markdown_links_case_and_punctuation():
    assert squash("Ada [Lovelace](https://x.y/z), **Countess**!") == "ada lovelace countess"


def test_normalise_relation():
    assert normalise_relation("Worked With") == "worked_with"


def test_validate_keeps_grounded_items_and_markdown_link_quotes():
    ledger = _ledger()
    ledger.validate(
        {
            "entities": [_entity("Ada Lovelace", GOOD_QUOTE), _entity("Charles Babbage", GOOD_QUOTE)],
            "relations": [_relation("Ada Lovelace", "Charles Babbage", GOOD_QUOTE)],
        }
    )
    assert [e.name for e in ledger.entities] == ["Ada Lovelace", "Charles Babbage"]
    assert [(r.source_name, r.type, r.target_name) for r in ledger.relations] == [("Ada Lovelace", "worked_with", "Charles Babbage")]
    assert ledger.dropped == []


def test_validate_drops_invented_quotes_names_missing_from_quote_and_bad_types():
    ledger = _ledger()
    ledger.validate(
        {
            "entities": [
                _entity("Ada Lovelace", GOOD_QUOTE),
                _entity("Grace Hopper", "Grace Hopper wrote the first compiler in 1952."),  # not on the page
                _entity("Charles Babbage", "The Analytical Engine was designed by Charles Babbage in London.", kind="wizard"),
                _entity("London", GOOD_QUOTE, kind="place"),  # real word, but not in this quote
                _entity("Ada Lovelace", GOOD_QUOTE),  # duplicate
                _entity("Babbage", "short"),
                _entity("Babbage", "Charles Babbage designed it.", source="https://evil.example/page"),
            ]
        }
    )
    assert [e.name for e in ledger.entities] == ["Ada Lovelace"]
    reasons = " | ".join(ledger.dropped)
    for expected in ("quote not found", "not allowed", "not in the quote", "duplicate", "too short", "was not fetched"):
        assert expected in reasons


def test_relation_needs_kept_endpoints_both_names_in_quote_and_a_clean_label():
    ledger = _ledger()
    ledger.validate(
        {
            "entities": [_entity("Ada Lovelace", GOOD_QUOTE), _entity("Charles Babbage", GOOD_QUOTE)],
            "relations": [
                _relation("Ada Lovelace", "Alan Turing", GOOD_QUOTE),  # endpoint never kept
                _relation("Ada Lovelace", "Charles Babbage", "The Analytical Engine was designed by Charles Babbage in London."),
                _relation("Ada Lovelace", "Charles Babbage", GOOD_QUOTE, rel="Worked With!!"),  # normalised, kept
                _relation("Ada Lovelace", "Ada Lovelace", GOOD_QUOTE),
                _relation("Ada Lovelace", "Charles Babbage", GOOD_QUOTE, rel="worked_with"),  # same edge again
            ],
        }
    )
    assert len(ledger.relations) == 1 and ledger.relations[0].type == "worked_with"
    reasons = " | ".join(ledger.dropped)
    assert "endpoint is not a kept entity" in reasons and "is not in the quote" in reasons and "self relation" in reasons


def test_validate_caps_entities():
    ledger = _ledger()
    ledger.pages[URL_A] = " ".join(f"Thing{i}x is notable." for i in range(MAX_ENTITIES + 5))
    ledger.validate({"entities": [_entity(f"Thing{i}x", f"Thing{i}x is notable.", kind="concept") for i in range(MAX_ENTITIES + 5)]})
    assert len(ledger.entities) == MAX_ENTITIES and any("cap" in d for d in ledger.dropped)


def test_validate_tolerates_garbage():
    ledger = _ledger()
    ledger.validate(None)
    ledger.validate({"entities": [{}], "relations": [{}]})
    assert ledger.entities == [] and ledger.relations == []


def _planned():
    ledger = _ledger()
    ledger.validate(
        {
            "entities": [_entity("Ada Lovelace", GOOD_QUOTE), _entity("Charles Babbage", GOOD_QUOTE)],
            "relations": [_relation("Ada Lovelace", "Charles Babbage", GOOD_QUOTE)],
        }
    )
    return ledger


def test_gate_entities_rewrites_content_and_blocks_strangers():
    ledger = _planned()
    kept = ledger.gate_entities(
        [
            {"name": "ada lovelace", "entityType": "concept", "observations": ["the model made this up"]},
            {"name": "Grace Hopper", "entityType": "person", "observations": ["x"]},
        ]
    )
    assert len(kept) == 1 and kept[0]["name"] == "Ada Lovelace" and kept[0]["entityType"] == "person"
    assert kept[0]["observations"][1].startswith("evidence: Ada Lovelace worked with")
    assert ledger.gate_blocks and "Grace Hopper" in ledger.gate_blocks[0]


def test_gate_relations_only_passes_planned_edges():
    ledger = _planned()
    kept = ledger.gate_relations(
        [
            {"from": "ada lovelace", "to": "Charles Babbage", "relationType": "Worked With"},
            {"from": "Ada Lovelace", "to": "Charles Babbage", "relationType": "married"},
        ]
    )
    assert kept == [{"from": "Ada Lovelace", "to": "Charles Babbage", "relationType": "worked_with"}]
    assert len(ledger.gate_blocks) == 1


def test_record_write_reads_server_responses_and_tracks_known_entities():
    ledger = _planned()
    ledger.record_write("create_entities", {"structuredContent": {"entities": [{"name": "Ada Lovelace", "entityType": "person"}]}})
    ledger.record_write(
        "open_nodes",
        {
            "content": [
                {
                    "type": "text",
                    "text": '{"entities":[{"name":"Ada Lovelace"},{"name":"Charles Babbage"}],'
                    '"relations":[{"from":"Ada Lovelace","to":"Charles Babbage","relationType":"worked_with"}]}',
                }
            ]
        },
    )
    assert ledger.verified and ledger.missing_entities() == [] and ledger.missing_relations() == []
    assert ledger.known_entities == {"charles babbage"}  # the server skipped it: it was already in the graph


def test_missing_items_are_reported_when_the_read_back_lacks_them():
    ledger = _planned()
    ledger.record_write("open_nodes", {"structuredContent": {"entities": [{"name": "Ada Lovelace"}], "relations": []}})
    assert ledger.missing_entities() == ["Charles Babbage"]
    assert ledger.missing_relations() == ["Ada Lovelace -worked_with-> Charles Babbage"]


def test_response_json_handles_text_and_garbage():
    assert response_json({"content": [{"text": '{"a": 1}'}]}) == {"a": 1}
    assert response_json("not json") is None


def test_render_report_has_tables_and_a_valid_looking_mermaid_graph():
    ledger = _planned()
    ledger.record_write("create_entities", {"structuredContent": {"entities": [{"name": "Ada Lovelace"}]}})
    ledger.record_write("open_nodes", {"structuredContent": {"entities": [{"name": "Ada Lovelace"}, {"name": "Charles Babbage"}],
                                                          "relations": [{"from": "Ada Lovelace", "to": "Charles Babbage", "relationType": "worked_with"}]}})
    report = render_report(ledger)
    assert "| Ada Lovelace | person | new |" in report and "| Charles Babbage | person | already known |" in report
    assert 'n0 -- "worked_with" --> n1' in report and "```mermaid" in report
    assert "2/2 entities and 1/1 relations present" in report


def test_render_report_without_entities_says_nothing_was_written():
    ledger = _ledger()
    assert "nothing was written" in render_report(ledger)


def test_reset_run_clears_per_request_state_but_keeps_the_meter():
    ledger = _planned()
    ledger.record_usage(10, 5)
    ledger.record_tool_call("fetch")
    ledger.reset_run([URL_B])
    assert ledger.urls == [URL_B] and ledger.entities == [] and ledger.pages == {} and ledger.tool_calls == 0
    assert ledger.prompt_tokens == 10 and ledger.model_calls == 1


def test_budget_and_cost():
    ledger = RunLedger(model="gemini-2.5-flash", max_tool_calls=1)
    assert ledger.record_tool_call("fetch") and not ledger.record_tool_call("fetch")
    ledger.record_usage(1_000_000, 1_000_000)
    assert round(ledger.cost_usd(), 2) == 2.80
    assert RunLedger(model="unlisted").cost_usd() is None
