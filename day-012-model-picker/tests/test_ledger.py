from datetime import date

from model_picker.ledger import (
    RUBRIC,
    RepoFacts,
    RunLedger,
    freshness_score,
    licence_score,
    parse_facts,
    popularity_score,
    render_report,
    score_candidates,
    weighted_total,
)

SEARCH = """Found 2 repositories across models matching query "x".

## Models (2)

### acme/tiny-embed

**Task:** sentence-similarity | **Library:** sentence-transformers | **Downloads:** 117.3K | **Likes:** 31

**Tags:** sentence-transformers, mteb, license:mit, region:us

**Created:** 27 Aug, 2025
**Link:** [https://hf.co/acme/tiny-embed](https://hf.co/acme/tiny-embed)

---

### acme/big-embed

**Task:** feature-extraction | **Library:** transformers | **Downloads:** 2.1M | **Likes:** 1,200

**Tags:** transformers, license:cc-by-nc-4.0

**Created:** 1 Jan, 2022
"""

DETAILS = """# acme/tiny-embed

## Overview
- **Downloads:** 120.0K | **Likes:** 35
- **Updated:** 3 Jul, 2026

## Metadata
- **License:** mit
"""

TODAY = date(2026, 10, 29)


def test_parse_search_results():
    tiny, big = parse_facts(SEARCH)
    assert (tiny.repo_id, tiny.downloads, tiny.likes, tiny.licence) == ("acme/tiny-embed", 117_300, 31, "mit")
    assert tiny.created == date(2025, 8, 27)
    assert (big.downloads, big.likes, big.licence) == (2_100_000, 1200, "cc-by-nc-4.0")


def test_parse_details_and_json_escaped_text():
    [facts] = parse_facts(DETAILS)
    assert (facts.downloads, facts.licence, facts.updated) == (120_000, "mit", date(2026, 7, 3))
    [again] = parse_facts(DETAILS.replace("\n", "\\n"))  # tool results arrive JSON-encoded
    assert again.downloads == 120_000


def test_evidence_merges_without_erasing():
    ledger = RunLedger(model="m")
    ledger.record_evidence(SEARCH)
    ledger.record_evidence(DETAILS)
    f = ledger.facts["acme/tiny-embed"]
    assert f.downloads == 120_000 and f.updated == date(2026, 7, 3) and f.created == date(2025, 8, 27)


def test_computed_criteria():
    assert [popularity_score(n) for n in (None, 100, 1_000, 10_000, 100_000, 10_000_000)] == [0, 0, 1, 2, 4, 5]
    assert freshness_score(date(2026, 9, 1), TODAY) == 5
    assert freshness_score(date(2025, 12, 1), TODAY) == 4
    assert freshness_score(date(2020, 1, 1), TODAY) == 1
    assert freshness_score(None, TODAY) == 0
    assert (licence_score("apache-2.0"), licence_score("cc-by-nc-4.0"), licence_score(None)) == (5, 2, 0)


def test_rubric_weights_sum_to_one():
    assert abs(sum(w for w, _ in RUBRIC.values()) - 1) < 1e-9


def test_weighted_total_is_a_0_100_scale():
    assert weighted_total({c: 5 for c in RUBRIC}) == 100.0
    assert weighted_total({c: 0 for c in RUBRIC}) == 0.0
    assert weighted_total({"task_fit": 5, "constraint_fit": 0, "popularity": 0, "licence": 0, "freshness": 0}) == 35.0


def _ledger():
    ledger = RunLedger(model="m")
    ledger.record_evidence(SEARCH)
    return ledger


def _j(repo_id, task=4, constraint=4, **kw):
    return {"repo_id": repo_id, "task_fit": task, "constraint_fit": constraint, "rationale": "r", "caveat": "", **kw}


def test_ranking_is_computed_and_sorted_by_code():
    # The model rates the big model's task fit higher, but the licence/freshness/popularity maths decides the order.
    ranked, out, problems = score_candidates(
        [_j("acme/big-embed", task=5, constraint=2), _j("acme/tiny-embed", task=4, constraint=5)], _ledger(), TODAY
    )
    assert not problems and not out
    assert [s.repo_id for s in ranked] == ["acme/tiny-embed", "acme/big-embed"]
    assert ranked[0].total > ranked[1].total
    assert ranked[0].scores["licence"] == 5 and ranked[1].scores["licence"] == 2


def test_candidate_never_returned_by_a_tool_is_a_problem_not_a_rank():
    ranked, _, problems = score_candidates([_j("acme/tiny-embed"), _j("made/up-model", task=5, constraint=5)], _ledger(), TODAY)
    assert [s.repo_id for s in ranked] == ["acme/tiny-embed"]
    assert any("made/up-model" in p and "never returned" in p for p in problems)


def test_repo_ids_match_case_insensitively_and_duplicates_are_flagged():
    _, _, problems = score_candidates([_j("ACME/Tiny-Embed"), _j("acme/tiny-embed")], _ledger(), TODAY)
    assert any("ranked twice" in p for p in problems)


def test_disqualified_candidates_are_excluded_from_the_ranking():
    ranked, out, problems = score_candidates([_j("acme/tiny-embed"), _j("acme/big-embed", disqualified=True)], _ledger(), TODAY)
    assert [s.repo_id for s in ranked] == ["acme/tiny-embed"] and out == ["acme/big-embed"] and not problems


def test_out_of_range_score_is_rejected():
    _, _, problems = score_candidates([_j("acme/tiny-embed", task=9)], _ledger(), TODAY)
    assert any("outside" in p for p in problems)


def test_report_shows_rubric_and_unknowns_honestly():
    ranked, out, _ = score_candidates([_j("acme/tiny-embed")], _ledger(), TODAY)
    ranked[0].facts = RepoFacts("acme/tiny-embed")  # nothing known
    report = render_report("need embeddings", ranked, out)
    assert "downloads unknown" in report and "licence not declared" in report
    assert "[acme/tiny-embed](https://huggingface.co/acme/tiny-embed)" in report and "computed from Hub data" in report


def test_tool_budget_and_usage_meter():
    ledger = RunLedger(model="m", max_tool_calls=2)
    assert ledger.record_tool_call("a") and ledger.record_tool_call("a") and not ledger.record_tool_call("b")
    assert ledger.calls_by_tool == {"a": 2}
    ledger.record_usage(10, 5)
    ledger.record_usage(None, None)
    assert (ledger.model_calls, ledger.prompt_tokens, ledger.output_tokens) == (2, 10, 5)
    assert RunLedger(model="unlisted").cost_usd() is None
