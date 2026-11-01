"""The eval set, defined as code and exported to evals/time_desk.evalset.json (the file `adk eval` reads).

Cases use zones without daylight saving so the expected answers stay correct all year.
"""

from __future__ import annotations

import json
from pathlib import Path

from google.adk.evaluation.eval_set import EvalSet

EVAL_SET_ID = "time_desk"
APP_NAME = "agent_evals"
EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"
EVALSET_PATH = EVALS_DIR / "time_desk.evalset.json"
CONFIG_PATH = EVALS_DIR / "test_config.json"

# Metric name -> pass threshold (average over cases and runs). Trajectory is exact; the response metric is
# ROUGE-1 against the reference sentence, loose enough to allow rewording.
CRITERIA = {"tool_trajectory_avg_score": 1.0, "response_match_score": 0.6}


def _convert(source: str, time: str, target: str) -> dict:
    return {"name": "convert_time", "args": {"source_timezone": source, "time": time, "target_timezone": target}}


# (eval_id, user message, expected tool calls in order, reference answer)
CASES: list[tuple[str, str, list[dict], str]] = [
    ("tokyo_to_kolkata", "What is 09:00 in Tokyo in Kolkata time?",
     [_convert("Asia/Tokyo", "09:00", "Asia/Kolkata")], "09:00 in Asia/Tokyo is 05:30 in Asia/Kolkata."),
    ("singapore_to_kathmandu", "Convert 14:00 Singapore time to Kathmandu time.",
     [_convert("Asia/Singapore", "14:00", "Asia/Kathmandu")], "14:00 in Asia/Singapore is 11:45 in Asia/Kathmandu."),
    ("dubai_to_karachi", "If it is 18:30 in Dubai, what time is it in Karachi?",
     [_convert("Asia/Dubai", "18:30", "Asia/Karachi")], "18:30 in Asia/Dubai is 19:30 in Asia/Karachi."),
    ("off_topic_no_tools", "What is the capital of France?",
     [], "I only help with time zone questions."),
    ("missing_zone_asks", "Convert 15:00 to Tokyo time.",
     [], "Which time zone is the 15:00 in?"),
]


def build_eval_set() -> EvalSet:
    cases = []
    for eval_id, question, tools, answer in CASES:
        cases.append({
            "eval_id": eval_id,
            "conversation": [{
                "invocation_id": f"{eval_id}-1",
                "user_content": {"role": "user", "parts": [{"text": question}]},
                "final_response": {"role": "model", "parts": [{"text": answer}]},
                "intermediate_data": {"tool_uses": tools, "tool_responses": [], "intermediate_responses": []},
            }],
            "session_input": {"app_name": APP_NAME, "user_id": "eval"},
        })
    return EvalSet.model_validate({
        "eval_set_id": EVAL_SET_ID,
        "name": "Time Desk",
        "description": "Conversions via the time MCP server, plus cases where no tool should be called.",
        "eval_cases": cases,
    })


def evalset_json() -> str:
    return json.dumps(build_eval_set().model_dump(mode="json", exclude_none=True), indent=2) + "\n"


def config_json() -> str:
    return json.dumps({"criteria": CRITERIA}, indent=2) + "\n"


def export() -> None:
    EVALS_DIR.mkdir(exist_ok=True)
    EVALSET_PATH.write_text(evalset_json())
    CONFIG_PATH.write_text(config_json())


if __name__ == "__main__":
    export()
    print(f"wrote {EVALSET_PATH} and {CONFIG_PATH}")
