import json

from hn_digest.ledger import (
    RunLedger,
    cited_ids,
    missing_sections,
    repeated_stories,
    request_from_message,
    stored_story_ids,
    unsaved_stories,
    unverified_links,
)

DIGEST = """# Hacker News digest · 2026-10-22
## Stories
1. **[PHP 8.2.35](https://github.com/php/php-src/releases/tag/php-8.2.35)** — A security release. · [864 points, 482 comments](https://news.ycombinator.com/item?id=50180873)
2. **[Other](https://example.com/other)** — Something. · [10 points, 2 comments](https://news.ycombinator.com/item?id=111)
## Memory
Skipped 1 already covered, saved 2.
"""
FRONT_PAGE = (
    '{"hits":[{"num_comments":482,"objectID":"50180873","points":864,"title":"PHP 8.2.35",'
    '"url":"https://github.com/php/php-src/releases/tag/php-8.2.35"},'
    '{"objectID":"111","title":"Other","url":"https://example.com/other"}]}'
)


def _memory(tmp_path, *names):
    path = tmp_path / "memory.jsonl"
    path.write_text("\n".join(json.dumps({"type": "entity", "name": n, "entityType": "hn_story", "observations": []}) for n in names))
    return path


def test_budget_blocks_after_limit():
    ledger = RunLedger(model="gemini-2.5-flash", max_tool_calls=2)
    assert ledger.record_tool_call("fetch")
    assert ledger.record_tool_call("open_nodes")
    assert not ledger.record_tool_call("create_entities")
    assert ledger.calls_by_tool == {"fetch": 1, "open_nodes": 1}


def test_cost_uses_price_table_and_unknown_model_has_none(monkeypatch):
    monkeypatch.delenv("GEMINI_PRICE_IN", raising=False)
    monkeypatch.delenv("GEMINI_PRICE_OUT", raising=False)
    ledger = RunLedger(model="gemini-2.5-flash")
    ledger.record_usage(1_000_000, 100_000)
    assert round(ledger.cost_usd(), 4) == 0.55
    assert RunLedger(model="some-new-model").cost_usd() is None


def test_request_needs_more_than_a_greeting():
    assert request_from_message("hello!") is None
    assert request_from_message("   ") is None
    assert request_from_message("5 stories about databases") == "5 stories about databases"


def test_stored_story_ids_reads_only_story_entities(tmp_path):
    path = _memory(tmp_path, "hn-1", "hn-22", "topic-ai")
    path.write_text(path.read_text() + '\n{"type":"relation","from":"hn-1","to":"topic-ai","relationType":"about"}\nnot json')
    assert stored_story_ids(path) == {"1", "22"}
    assert stored_story_ids(tmp_path / "missing.jsonl") == set()


def test_start_snapshots_memory_and_resets_run_state(tmp_path):
    ledger = RunLedger(model="m", memory_file=_memory(tmp_path, "hn-50180873"))
    ledger.record_tool_call("fetch")
    ledger.record_evidence("x")
    ledger.start("top stories")
    assert ledger.known_before == {"50180873"}
    assert ledger.tool_calls == 0 and ledger.evidence == []


def test_digest_structure_and_cited_ids():
    assert missing_sections(DIGEST) == []
    assert missing_sections("# only a title") == ["## Stories", "## Memory"]
    assert cited_ids(DIGEST) == {"50180873", "111"}


def test_links_must_come_from_tool_results():
    ledger = RunLedger(model="m")
    ledger.record_evidence(FRONT_PAGE)
    assert unverified_links(DIGEST, ledger) == []
    forged = DIGEST.replace("id=111", "id=999").replace("example.com/other", "evil.example/x")
    assert unverified_links(forged, ledger) == ["https://evil.example/x", "https://news.ycombinator.com/item?id=999"]


def test_repeats_are_judged_against_memory_before_the_run(tmp_path):
    ledger = RunLedger(model="m", memory_file=_memory(tmp_path, "hn-111"))
    ledger.start("top stories")
    assert repeated_stories(DIGEST, ledger) == ["111"]


def test_unsaved_stories_are_judged_against_memory_after_the_run(tmp_path):
    ledger = RunLedger(model="m", memory_file=_memory(tmp_path, "hn-50180873"))
    assert unsaved_stories(DIGEST, ledger) == ["111"]
