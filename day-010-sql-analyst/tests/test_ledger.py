from sql_analyst.ledger import RunLedger, normalize_sql, question_from_message, sql_blocks


def test_budget_blocks_after_limit_and_counts_by_tool():
    ledger = RunLedger(model="m", max_tool_calls=2)
    assert ledger.record_tool_call("list_tables") and ledger.record_tool_call("read_query")
    assert not ledger.record_tool_call("read_query")
    assert ledger.calls_by_tool == {"list_tables": 1, "read_query": 1}


def test_cost_uses_env_prices_or_none(monkeypatch):
    ledger = RunLedger(model="unlisted-model")
    ledger.record_usage(1_000_000, 1_000_000)
    monkeypatch.delenv("GEMINI_PRICE_IN", raising=False)
    assert ledger.cost_usd() is None
    monkeypatch.setenv("GEMINI_PRICE_IN", "0.1")
    monkeypatch.setenv("GEMINI_PRICE_OUT", "0.4")
    assert ledger.cost_usd() == 0.5


def test_shown_sql_must_match_executed_sql_ignoring_whitespace_case_and_semicolon():
    ledger = RunLedger(model="m")
    ledger.executed_sql.append("SELECT g.Name, COUNT(*) FROM Genre g GROUP BY 1")
    answer = "## SQL used\n```sql\nselect g.Name,\n  count(*) from Genre g group by 1;\n```\n"
    assert ledger.unexecuted_sql(answer) == []
    invented = answer + "```sql\nSELECT 42\n```\n"
    assert ledger.unexecuted_sql(invented) == ["SELECT 42"]


def test_helpers():
    assert normalize_sql(" SELECT  1 ;\n") == "select 1"
    assert sql_blocks("no sql here") == []
    assert question_from_message("hello") is None and question_from_message("Top artists?") == "Top artists?"


def test_start_resets_counters():
    ledger = RunLedger(model="m")
    ledger.record_tool_call("read_query")
    ledger.executed_sql.append("SELECT 1")
    ledger.start("next question")
    assert ledger.tool_calls == 0 and ledger.executed_sql == [] and ledger.question == "next question"
