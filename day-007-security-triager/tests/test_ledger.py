import json

from security_triager.ledger import RunLedger, check_report, extract_text, repo_from_message, verdict_counts


def _scan(*hits):
    return json.dumps(
        {"results": [{"check_id": r, "path": p, "start": {"line": n}, "extra": {"severity": "ERROR", "message": r}} for r, p, n in hits]}
    )


def test_budget_blocks_after_limit():
    ledger = RunLedger(model="m", max_tool_calls=2)
    assert ledger.record_tool_call("git_show") and ledger.record_tool_call("git_show")
    assert not ledger.record_tool_call("git_show")
    assert ledger.calls_by_tool == {"git_show": 2}


def test_cost_uses_price_table_and_env_override(monkeypatch):
    monkeypatch.delenv("GEMINI_PRICE_IN", raising=False)
    monkeypatch.delenv("GEMINI_PRICE_OUT", raising=False)
    ledger = RunLedger(model="gemini-2.5-flash")
    ledger.record_usage(1_000_000, 100_000)
    assert round(ledger.cost_usd(), 4) == 0.55
    assert RunLedger(model="some-new-model").cost_usd() is None
    monkeypatch.setenv("GEMINI_PRICE_IN", "1.0")
    monkeypatch.setenv("GEMINI_PRICE_OUT", "2.0")
    other = RunLedger(model="some-new-model")
    other.record_usage(1_000_000, 1_000_000)
    assert other.cost_usd() == 3.0


def test_scan_results_get_stable_ids_dedupe_and_code_excerpts():
    ledger = RunLedger(model="m")
    ledger.files["a.py"] = "import os\n\ndef run(x):\n    os.system(x)\n    return 1\n"
    added = ledger.add_scan(_scan(("py-os-command", "a.py", 4), ("py-weak-hash", "a.py", 1)))
    assert [f["id"] for f in added] == ["F1", "F2"]
    assert "4:     os.system(x)" in added[0]["code"]
    again = ledger.add_scan(_scan(("py-os-command", "a.py", 4), ("py-eval-exec", "a.py", 5)))
    assert [f["id"] for f in again] == ["F3"] and ledger.scans == 2 and len(ledger.findings) == 3


def test_report_check_passes_when_every_finding_has_a_verdict():
    ledger = RunLedger(model="m")
    ledger.add_scan(_scan(("r1", "a.py", 1), ("r2", "a.py", 2)))
    report = "| ID | Rule |\n|---|---|\n| F1 | r1 | a.py:1 | Exploitable | why | fix |\n| F2 | r2 | a.py:2 | false positive | why | fix |\n"
    checks = check_report(report, ledger)
    assert checks["all_findings_triaged"] and checks["no_invented_findings"] and checks["scan_ran"]


def test_report_check_flags_invented_and_missing_findings():
    ledger = RunLedger(model="m")
    ledger.add_scan(_scan(("r1", "a.py", 1), ("r2", "a.py", 2)))
    report = "| F1 | r1 | a.py:1 | exploitable | why | fix |\n| F9 | r9 | b.py:3 | likely | why | fix |\n| F2 | r2 | a.py:2 | maybe | why | fix |\n"
    checks = check_report(report, ledger)
    assert checks["invented_ids"] == ["F9"] and checks["untriaged_ids"] == ["F2"]
    assert not checks["no_invented_findings"] and not checks["all_findings_triaged"]


def test_report_check_requires_a_scan():
    assert check_report("Nothing to report.", RunLedger(model="m"))["scan_ran"] is False


def test_start_resets_per_run_state():
    ledger = RunLedger(model="m")
    ledger.start("/a")
    ledger.files["x.py"] = "1"
    ledger.record_tool_call("git_show")
    ledger.start("/b")
    assert ledger.repo == "/b" and ledger.files == {} and ledger.tool_calls == 0


def test_repo_comes_from_the_message():
    assert repo_from_message("Scan /srv/app please") == "/srv/app"
    assert repo_from_message("Repo: `/srv/other`.") == "/srv/other"
    assert repo_from_message("hello") is None and repo_from_message("scan my project") is None


def test_extract_text_handles_mcp_shapes():
    assert extract_text({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}) == "a\nb"
    assert extract_text("plain") == "plain"
    assert extract_text({"result": "x"}) == "x"


def test_verdicts_are_counted_from_the_table():
    report = "| ID | Rule | Location | Verdict | Why | Fix |\n|---|---|---|---|---|---|\n| F1 | r | a:1 | Exploitable | w | f |\n| F2 | r | a:2 | exploitable | w | f |\n| F3 | r | a:3 | false positive | w | f |\n"
    assert verdict_counts(report) == {"exploitable": 2, "likely": 0, "unlikely": 0, "false positive": 1}
