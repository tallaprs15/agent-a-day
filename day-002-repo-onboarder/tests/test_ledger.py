import pytest

from repo_onboarder.ledger import extract_repo, RunLedger, missing_sections, parse_repo, unverified_links


def test_budget_blocks_after_limit():
    ledger = RunLedger(model="gemini-2.5-flash", max_tool_calls=2)
    assert ledger.record_tool_call("ask_wiki_question")
    assert ledger.record_tool_call("fetch")
    assert not ledger.record_tool_call("fetch")
    assert ledger.calls_by_tool == {"ask_wiki_question": 1, "fetch": 1}


def test_cost_uses_price_table():
    ledger = RunLedger(model="gemini-2.5-flash")
    ledger.record_usage(1_000_000, 100_000)
    assert round(ledger.cost_usd(), 4) == 0.55  # 0.30 + 0.25


def test_unknown_model_has_no_cost(monkeypatch):
    monkeypatch.delenv("GEMINI_PRICE_IN", raising=False)
    monkeypatch.delenv("GEMINI_PRICE_OUT", raising=False)
    assert RunLedger(model="some-new-model").cost_usd() is None


@pytest.mark.parametrize(
    "value",
    ["owner/name", "https://github.com/owner/name", "https://github.com/owner/name/", "github.com/owner/name.git"],
)
def test_parse_repo_accepts_slug_and_urls(value):
    assert parse_repo(value) == "owner/name"


@pytest.mark.parametrize("value", ["name", "a/b/c", "owner/na me", "https://gitlab.com/o/n", ""])
def test_parse_repo_rejects_junk(value):
    with pytest.raises(ValueError):
        parse_repo(value)


def test_unverified_links_flags_invented_repo_paths_only():
    ledger = RunLedger(model="m", repo="o/r")
    ledger.record_evidence('{"result": "entry point is src/app.py"}')
    guide = (
        "[ok](https://github.com/o/r/blob/HEAD/src/app.py) "
        "[fake](https://github.com/o/r/blob/HEAD/src/made_up.py) "
        "[other](https://example.com/anything)"
    )
    assert unverified_links(guide, ledger) == ["https://github.com/o/r/blob/HEAD/src/made_up.py"]


def test_missing_sections():
    assert missing_sections("## What this project is\n## Gotchas") != []
    full = "\n".join(
        ["## What this project is", "## Architecture map", "## Where to start reading", "## Local setup",
         "## How to contribute", "## Gotchas"]
    )
    assert missing_sections(full) == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("can you fetch the details for tallaprs15/flickrole", "tallaprs15/flickrole"),
        ("Onboard me to https://github.com/psf/requests.git", "psf/requests"),
        ("look at github.com/modelcontextprotocol/python-sdk/tree/main", "modelcontextprotocol/python-sdk"),
        ("hello", None),
        ("compare 3/4 of the tests", None),
    ],
)
def test_extract_repo_from_free_text(text, expected):
    assert extract_repo(text) == expected
