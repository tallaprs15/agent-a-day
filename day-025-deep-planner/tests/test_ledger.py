from deep_planner.ledger import (
    Plan,
    RunLedger,
    Step,
    critical_path,
    graph_problems,
    parse_plan,
    render_markdown,
    request_from_message,
    thinking_problems,
    topological_order,
    unchecked_steps,
)


def step(i, deps=(), effort="M", done="A reviewer can open the file"):
    return Step(id=i, title=f"Do {i}", details="d", depends_on=list(deps), effort=effort, done_when=done)


def plan(*steps):
    return Plan(goal="g", steps=list(steps))


def test_budget_and_cost():
    ledger = RunLedger(model="gemini-2.5-flash", max_tool_calls=2)
    assert ledger.record_tool_call("t") and ledger.record_tool_call("t")
    assert not ledger.record_tool_call("t") and ledger.calls_by_tool == {"t": 2}
    ledger.record_usage(1_000_000, 1_000_000)
    assert ledger.cost_usd() == 0.30 + 2.50
    assert RunLedger(model="unknown").cost_usd() is None


def test_valid_graph_and_order():
    p = plan(step("S1"), step("S2", ["S1"]), step("S3", ["S1"]), step("S4", ["S2", "S3"]))
    assert graph_problems(p) == []
    order = topological_order(p)
    assert order.index("S1") < order.index("S2") < order.index("S4")


def test_graph_problems_are_reported():
    assert "S2 depends on unknown step S9" in graph_problems(plan(step("S1"), step("S2", ["S9"]), step("S3")))
    assert "S1 depends on itself" in graph_problems(plan(step("S1", ["S1"]), step("S2"), step("S3")))
    assert "duplicate id S1" in graph_problems(plan(step("S1"), step("S1"), step("S3")))
    assert "dependency cycle" in graph_problems(plan(step("S1", ["S3"]), step("S2", ["S1"]), step("S3", ["S2"])))
    assert any("steps (expected" in p for p in graph_problems(plan(step("S1"), step("S2"))))


def test_unchecked_steps():
    p = plan(step("S1", done="TBD"), step("S2", done=""), step("S3"))
    assert unchecked_steps(p) == ["S1", "S2"]


def test_critical_path_follows_the_longest_chain_by_effort():
    p = plan(step("S1", effort="S"), step("S2", ["S1"], "L"), step("S3", ["S1"], "S"), step("S4", ["S2", "S3"], "S"))
    path, days = critical_path(p)
    assert path == ["S1", "S2", "S4"] and days == 0.5 + 4 + 0.5


def test_thinking_must_be_stepwise_and_finished():
    ledger = RunLedger(model="m")
    assert len(thinking_problems(ledger)) == 2
    for n in range(3):
        ledger.record_thought({"nextThoughtNeeded": n < 2})
    assert thinking_problems(ledger) == []
    ledger.record_thought({"nextThoughtNeeded": True, "isRevision": True})
    assert ledger.revisions == 1 and thinking_problems(ledger) == ["reasoning never reached its final thought"]


def test_start_resets_everything_for_the_next_goal():
    ledger = RunLedger(model="m")
    ledger.record_tool_call("t")
    ledger.record_thought({"nextThoughtNeeded": False})
    ledger.start("another goal")
    assert (ledger.goal, ledger.tool_calls, ledger.thoughts, ledger.finished_thinking) == ("another goal", 0, 0, False)


def test_parse_and_render_round_trip():
    p = plan(step("S1"), step("S2", ["S1"]), step("S3", ["S2"]))
    assert parse_plan(p.model_dump_json()) == p
    assert parse_plan("not json") is None and parse_plan('{"goal": "x"}') is None
    md = render_markdown(p)
    assert "| S2 |" in md and "**Critical path:** S1 → S2 → S3" in md


def test_goal_needs_a_few_words():
    assert request_from_message("hi") is None and request_from_message("  ") is None
    assert request_from_message("Launch a personal blog") == "Launch a personal blog"
