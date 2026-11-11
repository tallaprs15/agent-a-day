"""Runs the real agent graph with a scripted stand-in for Gemini and the real arXiv MCP server.

This proves the wiring (state templating, the debate loop, output_schema, the reporter, the ledger and the
post-run checks) without an API key. It does not test model quality: that is what the live test is for.
"""

import asyncio
import json
import shutil

import pytest
from google.adk.models import BaseLlm, LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from paper_debate.agent import build_agent
from paper_debate.ledger import RunLedger, debate_checks

BRIEF = "Title: Attention Is All You Need\narXiv: 1706.03762\nClaims:\nC1: 28.4 BLEU on WMT14 En-De.\nC2: Trains faster than recurrent models."
RULING = {
    "claims": [
        {"claim_id": "C1", "verdict": "holds", "rationale": "Advocate cited the number; Skeptic did not contest it."},
        {"claim_id": "C2", "verdict": "partly", "rationale": "Speed claim lacks a like-for-like baseline."},
    ],
    "summary": "Quality claim is solid; speed claim needs care.",
    "confidence": "low",
}


class ScriptedLlm(BaseLlm):
    model: str = "scripted"

    async def generate_content_async(self, llm_request, stream=False):
        system = str(llm_request.config.system_instruction)
        has_tool_result = any(p.function_response for c in llm_request.contents for p in (c.parts or []))
        if "You are the reader" in system:
            if not has_tool_result:
                assert 'paper_id "1706.03762"' in system  # templated from state, not hard-coded
                part = types.Part.from_function_call(name="get_abstract", args={"paper_id": "1706.03762"})
            else:
                part = types.Part(text=BRIEF)
        elif "You are the Advocate" in system:
            assert "C1" in system and "Round" in system or "nothing has been said" in system
            part = types.Part(text="C1 holds: 28.4 BLEU. C2 holds.")
        elif "You are the Skeptic" in system:
            part = types.Part(text="C2: no like-for-like baseline. No outside evidence found.")
        else:
            assert "Round 2 · Skeptic" in system  # the judge sees the whole debate
            part = types.Part(text=json.dumps(RULING))
        yield LlmResponse(
            content=types.Content(role="model", parts=[part]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=100, candidates_token_count=10),
        )


@pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")
def test_full_graph_with_scripted_model():
    async def go():
        ledger = RunLedger(model="scripted")
        runner = InMemoryRunner(agent=build_agent(ledger, rounds=2, model=ScriptedLlm()), app_name="t")
        session = await runner.session_service.create_session(app_name="t", user_id="u")
        report = ""
        try:
            async for ev in runner.run_async(
                user_id="u", session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text="Paper: 1706.03762")]),
            ):
                if ev.author == "reporter":
                    report = ev.content.parts[0].text
            state = dict((await runner.session_service.get_session(app_name="t", user_id="u", session_id=session.id)).state)
        finally:
            await runner.close()
        return report, state, ledger

    report, state, ledger = asyncio.run(go())
    assert ledger.rounds == 2 and ledger.calls_by_tool == {"get_abstract": 1}
    assert all(debate_checks(state, ledger, 2).values())
    assert "| C2 | ⚠️ partly |" in report and report.count("· Advocate") == 2 and report.count("· Skeptic") == 2
