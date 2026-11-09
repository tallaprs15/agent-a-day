from a11y_auditor.ledger import (
    RunLedger,
    findings,
    missing_sections,
    rubric_problems,
    scorecard,
    target_from_message,
    unverified_evidence,
    unverified_pages,
)
from a11y_auditor.rubric import RUBRIC, in_scope

TARGET = "https://shop.example.com/"
# Evidence lines are written in the Fail rows' findings; every other criterion passes.
FAILS = {"1.1.1": "A11Y-1", "4.1.2": "A11Y-2"}


def make_report(fails=None, skip=(), extra_finding="", severity="serious"):
    fails = FAILS if fails is None else fails
    rows = ["| WCAG | Criterion | Result | Findings |", "|---|---|---|---|"]
    for c in RUBRIC:
        if c.id in skip:
            continue
        rows.append(f"| {c.id} | {c.name} | {'Fail' if c.id in fails else 'Pass'} | {fails.get(c.id, '-')} |")
    blocks = "".join(
        f"### {fid} · Problem {cid}\n- **WCAG:** {cid}\n- **Severity:** {severity}\n"
        f"- **Where:** {TARGET} header\n- **Evidence:** `button [ref=e7]`\n- **Fix:** add a name\n"
        for cid, fid in fails.items()
    )
    return "## Summary\nAudited.\n## Scorecard\n" + "\n".join(rows) + "\n## Findings\n" + blocks + extra_finding


def test_a_consistent_report_has_no_problems():
    report = make_report()
    assert missing_sections(report) == []
    assert rubric_problems(report) == []
    assert [r[0] for r in scorecard(report)] == [c.id for c in RUBRIC]
    assert sorted(findings(report)) == ["A11Y-1", "A11Y-2"]


def test_every_rubric_criterion_must_be_graded():
    assert rubric_problems(make_report(skip=("2.4.2",))) == ["2.4.2: not graded"]


def test_unknown_or_repeated_criteria_are_rejected():
    report = make_report().replace("| 2.4.2 |", "| 9.9.9 |", 1)
    assert "9.9.9: not in the rubric" in rubric_problems(report)
    assert "2.4.2: not graded" in rubric_problems(report)


def test_a_fail_without_a_finding_and_a_finding_without_a_fail_are_both_caught():
    no_finding = make_report().replace("| Fail | A11Y-2 |", "| Fail | - |")
    assert "4.1.2: marked Fail but lists no finding" in rubric_problems(no_finding)
    assert "A11Y-2: not listed under a Fail row in the scorecard" in rubric_problems(no_finding)


def test_a_finding_must_name_the_criterion_the_scorecard_gives_it():
    report = make_report().replace("- **WCAG:** 4.1.2", "- **WCAG:** 2.4.4")
    assert rubric_problems(report) == ["A11Y-2: names WCAG 2.4.4, scorecard says 4.1.2"]


def test_severity_must_come_from_the_scale():
    assert any("severity" in p for p in rubric_problems(make_report(severity="catastrophic")))


def test_evidence_must_appear_in_tool_output_and_exist():
    ledger = RunLedger(model="m", target_url=TARGET)
    ledger.record_evidence('- button [ref=e7]\n- link "Home" [ref=e3]')
    assert unverified_evidence(make_report(), ledger) == []
    ledger.evidence = ["- link [ref=e1]"]
    assert unverified_evidence(make_report(), ledger) == ["button [ref=e7]", "button [ref=e7]"]
    no_quote = make_report().replace("`button [ref=e7]`", "it is bad")
    assert unverified_evidence(no_quote, ledger) == ["A11Y-1: no quoted evidence", "A11Y-2: no quoted evidence"]


def test_pages_on_the_target_host_must_have_been_seen():
    ledger = RunLedger(model="m", target_url=TARGET)
    report = make_report() + "See https://shop.example.com/cart and https://w3.org/WAI/"
    ledger.record_evidence(f"Page URL: {TARGET}")
    assert unverified_pages(report, ledger) == ["https://shop.example.com/cart"]


def test_budgets_and_cost():
    ledger = RunLedger(model="m", max_tool_calls=1, max_pages=1)
    assert ledger.record_tool_call("a") and not ledger.record_tool_call("a")
    assert ledger.record_page(TARGET) and ledger.record_page(TARGET) and not ledger.record_page(TARGET + "x")
    ledger.record_usage(1000, 500)
    assert ledger.cost_usd({"m": (1.0, 2.0)}) == 0.002 and ledger.cost_usd({}) is None


def test_target_and_scope_rules():
    assert target_from_message("please audit https://a.example/x, thanks") == "https://a.example/x"
    assert target_from_message("no url here") is None
    assert in_scope("https://shop.example.com/cart", TARGET) and in_scope("https://cdn.shop.example.com/", "https://example.com")
    assert not in_scope("https://evil.net/", TARGET) and not in_scope("file:///etc/passwd", TARGET)
