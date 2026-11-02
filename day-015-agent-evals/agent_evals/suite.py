"""Run the eval set through ADK's evaluator and summarise the scores (pure helpers are unit-tested)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from google.adk.evaluation.base_eval_service import EvaluateConfig, EvaluateRequest, InferenceConfig, InferenceRequest
from google.adk.evaluation.eval_config import EvalConfig, get_eval_metrics_from_config
from google.adk.evaluation.eval_result import EvalCaseResult
from google.adk.evaluation.in_memory_eval_sets_manager import InMemoryEvalSetsManager
from google.adk.evaluation.local_eval_service import LocalEvalService
from google.adk.evaluation.simulation.user_simulator_provider import UserSimulatorProvider
from google.adk.utils.context_utils import Aclosing

from .cases import APP_NAME, CRITERIA, build_eval_set


@dataclass
class Scores:
    per_metric: dict[str, float] = field(default_factory=dict)  # mean score per metric
    per_case: dict[str, dict[str, float]] = field(default_factory=dict)  # eval_id -> metric -> mean score
    failed_cases: list[str] = field(default_factory=list)

    def checks(self, criteria: dict[str, float]) -> dict[str, bool]:
        return {m: self.per_metric.get(m, 0.0) >= t for m, t in criteria.items()}


def summarise(results: list[EvalCaseResult]) -> Scores:
    """Mean score per metric, overall and per case (a case run N times contributes N scores)."""
    by_metric: dict[str, list[float]] = defaultdict(list)
    by_case: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    failed: list[str] = []
    for case in results:
        if case.final_eval_status.name != "PASSED" and case.eval_id not in failed:
            failed.append(case.eval_id)
        for metric in case.overall_eval_metric_results:
            if metric.score is None or metric.metric_name not in CRITERIA:
                continue
            by_metric[metric.metric_name].append(metric.score)
            by_case[case.eval_id][metric.metric_name].append(metric.score)
    mean = lambda xs: sum(xs) / len(xs)  # noqa: E731
    return Scores(
        per_metric={m: mean(v) for m, v in by_metric.items()},
        per_case={c: {m: mean(v) for m, v in ms.items()} for c, ms in by_case.items()},
        failed_cases=failed,
    )


async def run_suite(num_runs: int = 1) -> Scores:
    """Inference + scoring with ADK's LocalEvalService (what `adk eval` uses underneath).

    Parallelism is 1 on purpose: the cases share one stdio MCP session, and the default of 4 concurrent cases
    closes that session mid-run ("Connection closed"). Sequential cases also keep the token meter simple."""
    from .agent import root_agent

    eval_set = build_eval_set()
    manager = InMemoryEvalSetsManager()
    manager.create_eval_set(app_name=APP_NAME, eval_set_id=eval_set.eval_set_id)
    for case in eval_set.eval_cases:
        manager.add_eval_case(app_name=APP_NAME, eval_set_id=eval_set.eval_set_id, eval_case=case)
    config = EvalConfig(criteria=CRITERIA)
    service = LocalEvalService(
        root_agent=root_agent,
        eval_sets_manager=manager,
        user_simulator_provider=UserSimulatorProvider(user_simulator_config=config.user_simulator_config),
    )
    request = InferenceRequest(
        app_name=APP_NAME, eval_set_id=eval_set.eval_set_id, inference_config=InferenceConfig(parallelism=1)
    )
    inferences = []
    for _ in range(num_runs):
        async with Aclosing(service.perform_inference(inference_request=request)) as stream:
            inferences.extend([item async for item in stream])
    evaluate = EvaluateRequest(
        inference_results=inferences,
        evaluate_config=EvaluateConfig(eval_metrics=get_eval_metrics_from_config(config), parallelism=1),
    )
    async with Aclosing(service.evaluate(evaluate_request=evaluate)) as stream:
        results = [item async for item in stream]
    return summarise(results)
