from meeting_planner.ledger import RunLedger, request_from_message


def test_budget_blocks_after_limit():
    ledger = RunLedger(model="m", max_tool_calls=2)
    assert ledger.record_tool_call("convert_time") and ledger.record_tool_call("convert_time")
    assert not ledger.record_tool_call("get_current_time")
    assert ledger.calls_by_tool == {"convert_time": 2}


def test_cost_uses_price_table_or_env(monkeypatch):
    ledger = RunLedger(model="gemini-2.5-flash-lite")
    ledger.record_usage(1_000_000, 1_000_000)
    assert round(ledger.cost_usd(), 2) == 0.50
    assert RunLedger(model="unknown").cost_usd() is None
    monkeypatch.setenv("GEMINI_PRICE_IN", "1")
    monkeypatch.setenv("GEMINI_PRICE_OUT", "2")
    ledger.model = "unknown"
    assert ledger.cost_usd() == 3.0


def test_start_resets_counters_for_a_new_request():
    ledger = RunLedger(model="m")
    ledger.record_tool_call("convert_time")
    ledger.zones_checked.add("Europe/London")
    ledger.start("another request")
    assert ledger.tool_calls == 0 and ledger.zones_checked == set() and ledger.request == "another request"


def test_greetings_are_not_requests():
    assert request_from_message("hi!") is None
    assert request_from_message("  ") is None
    assert request_from_message("30 min, London and Perth") == "30 min, London and Perth"
