import pytest

from model_router.ledger import LITE, STRONG, RunLedger, check_fetch_url, decide_route


def test_low_scores_route_to_lite():
    r = decide_route({"reasoning_depth": 1, "multi_step": 0, "precision_risk": 1, "rationale": "easy"}, limit=5)
    assert (r.tier, r.total, r.reason) == (LITE, 2, "easy")


def test_threshold_is_inclusive():
    scores = {"reasoning_depth": 2, "multi_step": 2, "precision_risk": 1}
    assert decide_route(scores, limit=5).tier == STRONG
    assert decide_route(scores, limit=6).tier == LITE


def test_threshold_from_env(monkeypatch):
    monkeypatch.setenv("ROUTE_THRESHOLD", "1")
    assert decide_route({"reasoning_depth": 1, "multi_step": 0, "precision_risk": 0}).tier == STRONG


@pytest.mark.parametrize(
    "bad",
    [None, "", {}, {"reasoning_depth": 1}, {"reasoning_depth": 9, "multi_step": 0, "precision_risk": 0},
     {"reasoning_depth": "x", "multi_step": 0, "precision_risk": 0}],
)
def test_unusable_router_output_fails_safe_to_strong(bad):
    r = decide_route(bad)
    assert r.tier == STRONG and r.total is None


@pytest.mark.parametrize("url", ["https://example.com/a", "http://docs.python.org"])
def test_public_urls_allowed(url):
    assert check_fetch_url(url) is None


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com", "http://localhost:8080", "http://127.0.0.1", "http://10.0.0.5/x",
            "http://192.168.1.1", "http://172.20.0.1", "http://169.254.169.254/latest", "http://[::1]/", "http://db.internal", ""]
)
def test_private_or_non_http_urls_blocked(url):
    assert check_fetch_url(url) is not None


def test_budget_blocks_after_limit():
    ledger = RunLedger(lite_model="a", strong_model="b", max_tool_calls=2)
    assert ledger.record_tool_call("fetch") and ledger.record_tool_call("fetch")
    assert not ledger.record_tool_call("fetch")
    assert ledger.calls_by_tool == {"fetch": 2}


def test_usage_is_tracked_per_model(monkeypatch):
    for k in ("LITE", "STRONG"):
        monkeypatch.delenv(f"GEMINI_PRICE_{k}_IN", raising=False)
        monkeypatch.delenv(f"GEMINI_PRICE_{k}_OUT", raising=False)
    ledger = RunLedger(lite_model="lite", strong_model="strong")
    ledger.record_usage("lite", 1000, 100)
    ledger.record_usage("strong", 2000, 300)
    ledger.record_usage("lite", None, None)
    assert ledger.model_calls == 3 and ledger.calls_by_model == {"lite": 2, "strong": 1}
    assert (ledger.prompt_tokens, ledger.output_tokens) == (3000, 400)
    assert ledger.cost_usd() is None  # no prices configured


def test_cost_sums_each_models_own_price(monkeypatch):
    monkeypatch.setenv("GEMINI_PRICE_LITE_IN", "0.1")
    monkeypatch.setenv("GEMINI_PRICE_LITE_OUT", "0.4")
    monkeypatch.setenv("GEMINI_PRICE_STRONG_IN", "1.0")
    monkeypatch.setenv("GEMINI_PRICE_STRONG_OUT", "5.0")
    ledger = RunLedger(lite_model="lite", strong_model="strong")
    ledger.record_usage("lite", 1_000_000, 0)
    ledger.record_usage("strong", 0, 1_000_000)
    assert round(ledger.cost_usd(), 4) == 5.1
