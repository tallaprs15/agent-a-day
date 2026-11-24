from pathlib import Path

from jd_matcher.ledger import (
    RunLedger,
    check_analysis,
    fit_score,
    invented_numbers,
    parse_request,
    quote_in,
    render_report,
    resolve_in_sandbox,
    verdict,
)

CV = "# Sam\n- Built a Python service that handles 40,000 shipments a day.\n- Led **12** services to Kubernetes."
JD = "## Requirements\n- 5+ years of Python experience.\n- Experience with Kubernetes and AWS."


def test_budget_and_cost():
    ledger = RunLedger(model="gemini-2.5-flash", max_tool_calls=2)
    assert ledger.record_tool_call("fetch") and ledger.record_tool_call("read_text_file")
    assert not ledger.record_tool_call("fetch")
    ledger.record_usage(1_000_000, 1_000_000)
    assert ledger.cost_usd() == 0.30 + 2.50
    assert RunLedger(model="unknown-model").cost_usd() is None


def test_parse_request_variants():
    assert parse_request("CV: cv.md  Job: job.md") == ("cv.md", "job.md")
    assert parse_request("resume sam/cv.txt and job https://example.com/jobs/1.") == ("sam/cv.txt", "https://example.com/jobs/1")
    assert parse_request("compare mine.md with theirs.md") == ("mine.md", "theirs.md")
    assert parse_request("please check my CV") is None
    assert parse_request("CV: cv.md  Job: cv.md") is None


def test_sandbox_blocks_escapes_and_other_file_types(tmp_path: Path):
    (tmp_path / "cv.md").write_text("x")
    assert resolve_in_sandbox("cv.md", tmp_path) == (tmp_path / "cv.md").resolve()
    assert resolve_in_sandbox("../etc/passwd.txt", tmp_path) is None
    assert resolve_in_sandbox("/etc/passwd.txt", tmp_path) is None
    assert resolve_in_sandbox("secrets.json", tmp_path) is None


def test_quote_matching_ignores_markdown_and_spacing():
    assert quote_in("Led 12 services to Kubernetes", CV)
    assert quote_in("built a python service that handles 40,000 shipments", CV)
    assert not quote_in("Led 20 services to Kubernetes", CV)
    assert not quote_in("go", CV)  # too short to prove anything


def test_invented_numbers_are_detected():
    assert invented_numbers("Handled 40,000 shipments a day", CV) == []
    assert invented_numbers("Handled 90,000 shipments a day", CV) == ["90,000"]


def _analysis(**over):
    base = {
        "requirements": [
            {"requirement": "Python", "jd_quote": "5+ years of Python experience", "importance": "must", "status": "met",
             "cv_evidence": "Built a Python service that handles 40,000 shipments a day"},
            {"requirement": "Kubernetes", "jd_quote": "Experience with Kubernetes and AWS", "importance": "must", "status": "partial",
             "cv_evidence": "Led 12 services to Kubernetes"},
            {"requirement": "Kafka", "jd_quote": "Experience with Kubernetes and AWS", "importance": "nice", "status": "gap", "cv_evidence": "made up"},
        ],
        "tailored_bullets": [{"bullet": "Shipped a Python service for 40,000 daily shipments", "cv_source": "Built a Python service that handles 40,000 shipments a day"}],
    }
    base.update(over)
    return base


def test_valid_analysis_passes_untouched():
    reqs, bullets, problems = check_analysis(_analysis(), CV, JD)
    assert problems == [] and len(reqs) == 3 and len(bullets) == 1
    assert reqs[2]["cv_evidence"] == ""  # a gap never keeps evidence


def test_fabricated_claims_are_demoted_or_dropped():
    bad = _analysis()
    bad["requirements"][0]["cv_evidence"] = "Ten years of Python at Google"  # not in the CV
    bad["requirements"][1]["jd_quote"] = "Must hold a PhD in physics"  # not in the JD
    bad["tailored_bullets"].append({"bullet": "Cut costs by 70%", "cv_source": "Led 12 services to Kubernetes"})
    bad["tailored_bullets"].append({"bullet": "Won an award", "cv_source": "Won the company hackathon"})
    reqs, bullets, problems = check_analysis(bad, CV, JD)
    assert [r["requirement"] for r in reqs] == ["Python", "Kafka"]
    assert reqs[0]["status"] == "gap"  # demoted: evidence was invented
    assert len(bullets) == 1
    assert len(problems) == 4


def test_score_weights_musts_double_and_verdict_flags_must_gaps():
    reqs = [
        {"importance": "must", "status": "met"},
        {"importance": "must", "status": "gap"},
        {"importance": "nice", "status": "partial"},
    ]
    assert fit_score(reqs) == round(100 * (2 + 0 + 0.5) / 5, 1)
    assert fit_score([]) is None
    assert verdict(50.0, reqs) == "Partial match (1 must-have gap)"
    assert verdict(90.0, [{"importance": "must", "status": "met"}]) == "Strong match"
    assert verdict(20.0, reqs) == "Weak match"


def test_report_shows_evidence_gaps_and_integrity_notes():
    reqs, bullets, problems = check_analysis(_analysis(), CV, JD)
    report = render_report("Platform Engineer", "Good fit.", reqs, bullets, fit_score(reqs), ["something was dropped"])
    assert "Fit score" in report and "none found" in report and "Integrity notes" in report
    assert "Led 12 services to Kubernetes" in report
