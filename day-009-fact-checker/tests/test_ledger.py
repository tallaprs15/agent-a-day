from fact_checker.ledger import RunLedger, clean_url, count_claims, normalise, report_problems, wikipedia_url

GOOD = """# Fact check
### Claim 1: Sydney is the capital of Australia.
**Verdict: Refuted**
Canberra is the capital.
- [Canberra](https://en.wikipedia.org/wiki/Canberra)
  > "Canberra (/ˈkænbrə/ ⓘ KAN-brə) is the capital city of Australia"

### Claim 2: The sky is green.
**Verdict: Unverifiable**
Nothing returned.
## Sources
- https://en.wikipedia.org/wiki/Canberra
"""


def _ledger_with_evidence():
    ledger = RunLedger(model="m")
    ledger.record_evidence('{"summary": "Canberra (/ ˈkænbrə / ⓘ KAN-brə)\\nis the capital city of Australia and more."}')
    ledger.record_wikipedia_page("Canberra")
    return ledger


def test_total_budget_is_enforced():
    ledger = RunLedger(model="m", max_tool_calls=2, max_calls_per_round=5)
    assert ledger.record_tool_call("search") is None
    assert ledger.record_tool_call("get_summary") is None
    assert "Total tool budget" in ledger.record_tool_call("search")
    assert ledger.tool_calls == 2 and ledger.calls_by_tool == {"search": 1, "get_summary": 1}


def test_round_budget_resets_each_round_but_total_does_not():
    ledger = RunLedger(model="m", max_tool_calls=3, max_calls_per_round=2)
    ledger.start_round()
    assert ledger.record_tool_call("search") is None and ledger.record_tool_call("search") is None
    assert "round's budget" in ledger.record_tool_call("search")
    ledger.start_round()
    assert ledger.record_tool_call("search") is None  # fresh round allowance
    assert "Total tool budget" in ledger.record_tool_call("search")  # but the run total is spent
    assert ledger.rounds == 2


def test_usage_and_cost():
    ledger = RunLedger(model="gemini-2.5-flash")
    ledger.record_usage(1_000_000, 100_000)
    ledger.record_usage(None, None)
    assert ledger.model_calls == 2
    assert abs(ledger.cost_usd() - 0.55) < 1e-9
    assert RunLedger(model="unknown").cost_usd() is None


def test_urls_and_wikipedia_pages():
    assert clean_url("https://a.org/x/#frag.") == "https://a.org/x"
    assert wikipedia_url("Mount Everest") == "https://en.wikipedia.org/wiki/Mount_Everest"


def test_quotes_compare_by_words_not_spacing_or_punctuation():
    assert normalise("Canberra (/ˈkænbrə/ ⓘ) is") == normalise("canberra  /ˈkænbrə/ ⓘ is")


def test_well_formed_report_has_no_problems():
    assert report_problems(GOOD, _ledger_with_evidence(), expected_claims=2) == []


def test_missing_verdict_and_wrong_claim_count_are_flagged():
    bad = GOOD.replace("**Verdict: Refuted**", "Refuted.")
    problems = report_problems(bad, _ledger_with_evidence(), expected_claims=3)
    assert any("Expected 3" in p for p in problems) and any("Claim 1 has no" in p for p in problems)


def test_verdict_without_a_source_is_flagged():
    bad = GOOD.replace("- [Canberra](https://en.wikipedia.org/wiki/Canberra)\n  > \"Canberra (/ˈkænbrə/ ⓘ KAN-brə) is the capital city of Australia\"\n", "")
    assert any("cites no source" in p for p in report_problems(bad, _ledger_with_evidence()))


def test_invented_link_is_flagged():
    bad = GOOD + "- https://example.com/made-up\n"
    assert any("example.com/made-up" in p for p in report_problems(bad, _ledger_with_evidence()))


def test_fabricated_quote_is_flagged_but_a_faithful_one_passes():
    bad = GOOD.replace("is the capital city of Australia", "is the largest city of Australia")
    assert any("Quoted evidence not found" in p for p in report_problems(bad, _ledger_with_evidence()))


def test_count_claims_strips_bullets_and_blank_lines():
    assert count_claims("- one claim here\n\n2. another claim\nthird") == 3
