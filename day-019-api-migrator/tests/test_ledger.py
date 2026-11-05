from api_migrator.ledger import (
    RunLedger,
    breaking_change_rows,
    missing_sections,
    request_from_message,
    side_of,
    ungrounded_calls,
    ungrounded_symbols,
    unverified_sources,
)

PLAN = """\
## Summary
Migrates the SDK.

## Breaking changes
| Old API | New API | Evidence |
|---|---|---|
| `BlockBlobService` | `BlobServiceClient` | https://learn.microsoft.com/a |
| `create_blob_from_path` | `upload_blob` | "upload_blob(data)" |
| `get_blob_to_path` | removed | n/a |

## Migration steps
1. Replace the client.

## Sources
- https://learn.microsoft.com/a
"""


def _ledger():
    ledger = RunLedger(model="gemini-2.5-flash")
    ledger.record_evidence("old", "BlockBlobService create_blob_from_path get_blob_to_path https://learn.microsoft.com/a")
    ledger.record_evidence("new", "BlobServiceClient upload_blob(data)")
    return ledger


def test_budget_blocks_after_limit():
    ledger = RunLedger(model="gemini-2.5-flash", max_tool_calls=2)
    assert ledger.record_tool_call("query-docs") and ledger.record_tool_call("query-docs")
    assert not ledger.record_tool_call("resolve-library-id")
    assert ledger.calls_by_tool == {"query-docs": 2}


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


def test_request_needs_a_library_and_two_versions():
    assert request_from_message("hello there") is None
    assert request_from_message("Migrate Pydantic please") is None
    assert request_from_message("Migrate Pydantic from v1 to v2")
    assert request_from_message("Move requests from 2.0 to latest")
    assert request_from_message("") is None


def test_side_of_agent_name():
    assert side_of("old_researcher") == "old" and side_of("new_researcher") == "new"
    assert side_of("differ") is None


def test_breaking_change_rows_skip_header_and_separator():
    assert breaking_change_rows(PLAN) == [
        ("`BlockBlobService`", "`BlobServiceClient`"),
        ("`create_blob_from_path`", "`upload_blob`"),
        ("`get_blob_to_path`", "removed"),
    ]


def test_grounded_plan_passes_all_checks():
    ledger = _ledger()
    assert ungrounded_symbols(PLAN, ledger) == []
    assert unverified_sources(PLAN, ledger) == []
    assert missing_sections(PLAN) == []
    assert missing_sections("## Summary only") == ["## Breaking changes", "## Migration steps", "## Sources"]


def test_symbol_must_come_from_the_matching_side():
    """A new API that only the OLD docs mention (or an old API only in the new docs) is a diff error."""
    ledger = RunLedger(model="m")
    ledger.record_evidence("old", "BlockBlobService upload_blob")
    ledger.record_evidence("new", "BlobServiceClient")
    plan = "## Breaking changes\n| Old API | New API | Evidence |\n|---|---|---|\n| `BlobServiceClient` | `upload_blob` | x |\n"
    assert ungrounded_symbols(plan, ledger) == ["new:upload_blob", "old:BlobServiceClient"]


def test_invented_symbols_and_sources_are_reported():
    ledger = _ledger()
    plan = PLAN.replace("`BlobServiceClient`", "`MadeUpClient`").replace("https://learn.microsoft.com/a\n", "https://example.com/fake\n")
    assert ungrounded_symbols(plan, ledger) == ["new:MadeUpClient"]
    assert unverified_sources(plan, ledger) == ["https://example.com/fake"]


def test_code_calls_checked_against_both_sides():
    ledger = _ledger()
    ok = "```python\nclient = BlobServiceClient()\nclient.upload_blob(data)\n```"
    bad = "```python\nclient.teleport_blob(data)\n```"
    assert ungrounded_calls(ok, ledger) == []
    assert ungrounded_calls(bad, ledger) == ["teleport_blob"]


def test_start_resets_budget_and_evidence():
    ledger = _ledger()
    ledger.record_tool_call("query-docs")
    ledger.start("Migrate X from 1 to 2")
    assert ledger.tool_calls == 0 and ledger.seen() == "" and ledger.request == "Migrate X from 1 to 2"
