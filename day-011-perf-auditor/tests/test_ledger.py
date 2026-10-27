import pytest

from perf_auditor.ledger import RunLedger, extract_url, rate, report_checks, report_problems, url_problem

TRACE = """URL: https://example.com/
  - LCP: 3100 ms, event: (eventKey: r-1706)
    - TTFB: 4 ms
    - Render delay: 311 ms
  - CLS: 0.04
  - INP: 520 ms
"""

GOOD = """# Performance audit: https://example.com
## Core Web Vitals
| Metric | Value | Rating |
|---|---|---|
| LCP | 3100 ms | Needs improvement |
| CLS | 0.04 | Good |
| INP | 520 ms | Poor |
| TTFB | 4 ms | Good |
## Diagnosis
Render delay was 311 ms of the LCP.
## Recommendations
1. Reduce render delay.
"""


def _ledger():
    ledger = RunLedger(model="gemini-2.5-flash")
    ledger.record_tool_call("performance_start_trace")
    ledger.record_tool_call("performance_analyze_insight")
    ledger.record_evidence(TRACE)
    return ledger


def test_budget_refuses_after_max():
    ledger = RunLedger(model="m", max_tool_calls=2)
    assert ledger.record_tool_call("a") is None
    assert ledger.record_tool_call("b") is None
    assert "budget" in ledger.record_tool_call("c")
    assert ledger.tool_calls == 2 and ledger.calls_by_tool == {"a": 1, "b": 1}


def test_cost_uses_known_price_or_none(monkeypatch):
    monkeypatch.delenv("GEMINI_PRICE_IN", raising=False)
    ledger = RunLedger(model="gemini-2.5-flash-lite")
    ledger.record_usage(1_000_000, 1_000_000)
    assert ledger.cost_usd() == pytest.approx(0.50)
    assert RunLedger(model="unknown").cost_usd() is None
    monkeypatch.setenv("GEMINI_PRICE_IN", "1")
    monkeypatch.setenv("GEMINI_PRICE_OUT", "2")
    assert ledger.cost_usd() == pytest.approx(3.0)


@pytest.mark.parametrize(
    "metric,value,expected",
    [("LCP", 2500, "Good"), ("LCP", 2501, "Needs improvement"), ("LCP", 4001, "Poor"), ("CLS", 0.1, "Good"), ("CLS", 0.26, "Poor"), ("INP", 200, "Good")],
)
def test_rate_uses_threshold_boundaries(metric, value, expected):
    assert rate(metric, value) == expected


def test_evidence_collects_ms_and_cls():
    ledger = _ledger()
    assert ledger.has_ms(3100) and ledger.has_ms(311.4) and not ledger.has_ms(1200)
    assert ledger.has_cls(0.04) and not ledger.has_cls(0.3)


@pytest.mark.parametrize(
    "text,url",
    [("Audit https://web.dev/learn.", "https://web.dev/learn"), ("check web.dev please", "https://web.dev"), ("hello", None)],
)
def test_extract_url(text, url):
    assert extract_url(text) == url


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "http://localhost:3000", "http://127.0.0.1", "http://10.0.0.5/admin", "http://169.254.169.254/latest", "http://[::1]/", "https://user:pw@example.com", "http://intranet", "http://printer.local"],
)
def test_url_problem_blocks_unsafe_targets(url):
    assert url_problem(url)


def test_url_problem_allows_public_pages():
    assert url_problem("https://web.dev") is None
    assert url_problem("http://93.184.216.34/") is None


def test_good_report_passes_all_checks():
    checks = report_checks(GOOD, _ledger())
    assert all(not v for v in checks.values()), checks


def test_invented_metric_is_rejected():
    bad = GOOD.replace("3100 ms", "2900 ms")
    assert any("2900" in p for p in report_checks(bad, _ledger())["numbers_verified"])


def test_wrong_rating_is_rejected():
    bad = GOOD.replace("| 3100 ms | Needs improvement |", "| 3100 ms | Good |")
    assert any("LCP" in p for p in report_checks(bad, _ledger())["ratings_correct"])


def test_invented_figure_in_prose_is_rejected():
    bad = GOOD.replace("311 ms", "950 ms")
    assert any("950" in p for p in report_problems(bad, _ledger()))


def test_threshold_figures_may_be_quoted():
    ok = GOOD.replace("Reduce render delay.", "Good is up to 2500 ms.")
    assert report_problems(ok, _ledger()) == []


def test_not_measured_rows_are_allowed():
    ok = GOOD.replace("| INP | 520 ms | Poor |", "| INP | not measured | n/a |")
    assert report_problems(ok, _ledger()) == []


def test_missing_sections_and_trace_are_reported():
    ledger = RunLedger(model="m")
    checks = report_checks("# Performance audit\nfast!", ledger)
    assert checks["structure"] and checks["trace_recorded"]


def test_report_without_insight_analysis_is_flagged():
    ledger = RunLedger(model="m")
    ledger.record_tool_call("performance_start_trace")
    ledger.record_evidence(TRACE)
    assert "insight" in report_checks(GOOD, ledger)["trace_recorded"][0]
