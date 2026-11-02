from types import SimpleNamespace as NS

from agent_evals.suite import Scores, summarise


def _case(eval_id, status, **scores):
    return NS(
        eval_id=eval_id,
        final_eval_status=NS(name=status),
        overall_eval_metric_results=[NS(metric_name=m, score=s) for m, s in scores.items()],
    )


def test_summarise_means_per_metric_and_per_case_and_lists_failures():
    results = [
        _case("a", "PASSED", tool_trajectory_avg_score=1.0, response_match_score=0.8),
        _case("a", "FAILED", tool_trajectory_avg_score=0.0, response_match_score=0.6),  # second run of "a"
        _case("b", "PASSED", tool_trajectory_avg_score=1.0, response_match_score=1.0, token_usage_v1=500.0),
    ]
    scores = summarise(results)
    assert scores.per_case["a"] == {"tool_trajectory_avg_score": 0.5, "response_match_score": 0.7}
    assert round(scores.per_metric["tool_trajectory_avg_score"], 3) == 0.667
    assert "token_usage_v1" not in scores.per_metric  # only metrics with a threshold are reported
    assert scores.failed_cases == ["a"]


def test_checks_compare_means_to_thresholds_and_missing_metric_fails():
    scores = Scores(per_metric={"x": 0.9})
    assert scores.checks({"x": 0.9, "y": 0.1}) == {"x": True, "y": False}
