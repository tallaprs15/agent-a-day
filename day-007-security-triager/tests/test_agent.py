import asyncio
import json
import os
import shutil
import subprocess
import sys

import pytest

from security_triager.agent import (
    ASK_FOR_REPO,
    GIT_TOOLS,
    MAX_FILES_PER_SCAN,
    SCAN_TOOL,
    SEMGREP_TOOLS,
    _git_toolset,
    _make_callbacks,
    _semgrep_toolset,
)
from security_triager.ledger import MAX_FILE_CHARS, RunLedger
from security_triager.rules import RULE_IDS, RULES_YAML


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    def __init__(self, text):
        from google.genai import types

        self.user_content = types.Content(role="user", parts=[types.Part(text=text)])
        self.state = {}


def _callbacks(tmp_path, **kwargs):
    ledger = RunLedger(model="m", repo=str(tmp_path), **kwargs)
    resolve_repo, before_tool, after_tool, _ = _make_callbacks(ledger)
    return ledger, resolve_repo, before_tool, after_tool


def test_git_show_is_pinned_to_the_repo_and_to_head_paths(tmp_path):
    ledger, _, before_tool, _ = _callbacks(tmp_path, max_tool_calls=5)
    args = {"repo_path": "/etc", "revision": "HEAD:app.py"}
    assert before_tool(_Tool("git_show"), args, None) is None
    assert args["repo_path"] == str(tmp_path)
    for bad in ("HEAD~5", "main", "HEAD:../secrets.py", "--output=/tmp/x"):
        assert "revision" in before_tool(_Tool("git_show"), {"revision": bad}, None)["error"]
    assert ledger.tool_calls == 1  # rejected calls cost no budget


def test_scan_gets_cached_content_and_the_bundled_rules(tmp_path):
    ledger, _, before_tool, _ = _callbacks(tmp_path)
    ledger.files["app/views.py"] = "import os\nos.system(x)\n"
    args = {"code_files": [{"path": "./app/views.py", "content": ""}], "rule": "rules: []  # model-written"}
    assert before_tool(_Tool(SCAN_TOOL), args, None) is None
    assert args["code_files"] == [{"path": "app/views.py", "content": "import os\nos.system(x)\n"}]
    assert args["rule"] == RULES_YAML


def test_scan_rejects_unread_files_and_oversized_batches(tmp_path):
    ledger, _, before_tool, _ = _callbacks(tmp_path)
    ledger.files["a.py"] = "x = 1\n"
    unread = before_tool(_Tool(SCAN_TOOL), {"code_files": [{"path": "a.py"}, {"path": "b.py"}]}, None)
    assert "b.py" in unread["error"] and "before scanning" in unread["error"]
    many = {"code_files": [{"path": f"f{i}.py"} for i in range(MAX_FILES_PER_SCAN + 1)]}
    assert "between 1 and" in before_tool(_Tool(SCAN_TOOL), many, None)["error"]
    assert ledger.tool_calls == 0


def test_budget_is_enforced(tmp_path):
    ledger, _, before_tool, _ = _callbacks(tmp_path, max_tool_calls=1)
    assert before_tool(_Tool("git_show"), {"revision": "HEAD:"}, None) is None
    assert "budget" in before_tool(_Tool("git_show"), {"revision": "HEAD:"}, None)["error"]


def test_after_tool_caches_source_files_and_truncates_big_ones(tmp_path):
    ledger, _, _, after_tool = _callbacks(tmp_path)
    assert after_tool(_Tool("git_show"), {"revision": "HEAD:a.py"}, None, {"content": [{"text": "x = 1\n"}]}) is None
    assert ledger.files["a.py"] == "x = 1\n"
    assert after_tool(_Tool("git_show"), {"revision": "HEAD:"}, None, {"content": [{"text": "a.py\n"}]}) is None
    big = after_tool(_Tool("git_show"), {"revision": "HEAD:big.py"}, None, {"content": [{"text": "y" * (MAX_FILE_CHARS + 50)}]})
    assert "truncated" in big["result"] and len(ledger.files["big.py"]) == MAX_FILE_CHARS
    assert "a.py" in ledger.files and "" not in ledger.files


def test_after_tool_turns_a_scan_into_compact_findings(tmp_path):
    ledger, _, _, after_tool = _callbacks(tmp_path)
    ledger.files["a.py"] = "import os\nos.system(x)\n"
    scan = {"results": [{"check_id": "py-os-command", "path": "a.py", "start": {"line": 2}, "extra": {"severity": "ERROR", "message": "m", "lines": "requires login"}}]}
    reply = after_tool(_Tool(SCAN_TOOL), {}, None, {"content": [{"text": json.dumps(scan)}]})
    assert reply["count"] == 1 and reply["findings"][0]["id"] == "F1" and "os.system" in reply["findings"][0]["code"]
    assert "error" in after_tool(_Tool(SCAN_TOOL), {}, None, {"content": [{"text": "boom"}]})


def test_repo_comes_from_the_message_not_the_code(tmp_path):
    """Two different repositories in one session bind to two different states; a greeting or bad path asks instead."""
    first, second = tmp_path / "one", tmp_path / "two"
    for repo in (first, second):
        (repo / ".git").mkdir(parents=True)
    ledger, resolve_repo, _, _ = _callbacks(tmp_path)
    a, b = _Ctx(f"Scan {first}"), _Ctx(f"Now scan {second}")
    assert resolve_repo(a) is None and resolve_repo(b) is None
    assert a.state["repo"] == str(first) and b.state["repo"] == str(second) and ledger.repo == str(second)
    assert resolve_repo(_Ctx("hello")).parts[0].text == ASK_FOR_REPO
    assert "not the root of a git checkout" in resolve_repo(_Ctx(f"Scan {tmp_path / 'nope'}")).parts[0].text


needs_uvx = pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")

RULE_SAMPLES = {
    "samples.py": (
        "import os, subprocess, pickle, yaml, hashlib, requests\n"
        "subprocess.call(cmd, shell=True)\n"
        "os.system(cmd)\n"
        "eval(expr)\n"
        "pickle.loads(blob)\n"
        "yaml.load(text)\n"
        "hashlib.md5(b'x')\n"
        "requests.get(url, verify=False)\n"
        "app.run(debug=True)\n"
        "cur.execute('SELECT * FROM t WHERE id = ' + uid)\n"
        "password = 'changeme'\n"
    ),
    "samples.js": (
        "eval(input);\n"
        "el.innerHTML = value;\n"
        "cp.exec('ls ' + dir);\n"
    ),
}


@needs_uvx
@pytest.mark.parametrize("factory,expected", [(_git_toolset, GIT_TOOLS), (_semgrep_toolset, SEMGREP_TOOLS)])
def test_mcp_servers_expose_exactly_the_filtered_tools(factory, expected):
    """Starts the real MCP servers (no API key needed) and checks least-privilege filtering."""

    async def names():
        toolset = factory()
        try:
            return sorted(t.name for t in await toolset.get_tools())
        finally:
            await toolset.close()

    assert asyncio.run(names()) == sorted(expected)


@needs_uvx
def test_bundled_rule_pack_fires_every_rule_through_the_real_semgrep_server():
    """Runs the real scan tool (offline, no API key) and checks each bundled rule matches its sample."""

    async def scan():
        toolset = _semgrep_toolset()
        try:
            tool = next(t for t in await toolset.get_tools() if t.name == SCAN_TOOL)
            files = [{"path": p, "content": c} for p, c in RULE_SAMPLES.items()]
            return await tool.run_async(args={"code_files": files, "rule": RULES_YAML}, tool_context=None)
        finally:
            await toolset.close()

    from security_triager.ledger import extract_text

    results = json.loads(extract_text(asyncio.run(scan())))["results"]
    assert {r["check_id"] for r in results} == set(RULE_IDS)


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="live test needs GOOGLE_API_KEY")
def test_live_demo_triage_covers_every_finding(tmp_path):
    out, metrics = tmp_path / "triage.md", tmp_path / "m.json"
    result = subprocess.run(
        [sys.executable, "-m", "security_triager", "--demo", "-o", str(out), "--metrics", str(metrics)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(metrics.read_text())
    assert data["checks"]["findings"] >= 5 and data["checks"]["all_findings_triaged"] is True
    assert "## Findings" in out.read_text()
