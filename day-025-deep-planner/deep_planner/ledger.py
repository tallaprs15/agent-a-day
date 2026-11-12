"""Per-run bookkeeping (tool budget, token meter, thought trace) and the deterministic plan validators.

One RunLedger is shared by every stage's callbacks. The thought trace records what the reasoning tool
was actually asked to do, so the CLI can check the plan came out of real step-by-step thinking.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

# Approximate list prices in USD per 1M tokens (input, output), for budgeting,
# not billing. For models not listed, set GEMINI_PRICE_IN / GEMINI_PRICE_OUT.
DEFAULT_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

MIN_THOUGHTS = 3
MIN_STEPS, MAX_STEPS = 3, 15


class Step(BaseModel):
    id: str = Field(description="Unique id such as S1, S2, S3")
    title: str = Field(description="Imperative, under 80 characters")
    details: str = Field(description="What to do, in one or two sentences")
    depends_on: list[str] = Field(default_factory=list, description="Ids of steps that must finish first")
    effort: str = Field(description="One of S (under half a day), M (1-2 days), L (3+ days)")
    done_when: str = Field(description="An observable acceptance check for this step")


class Plan(BaseModel):
    goal: str = Field(description="The goal restated precisely, one sentence")
    assumptions: list[str] = Field(default_factory=list)
    steps: list[Step]
    risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


def request_from_message(text: str) -> str | None:
    """A goal needs a few words. A greeting or a single word is not enough to plan anything."""
    text = (text or "").strip()
    return text if len(text.split()) >= 3 else None


@dataclass
class RunLedger:
    model: str
    goal: str = ""
    max_tool_calls: int = 10
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    thoughts: int = 0  # reasoning-tool calls that were accepted
    revisions: int = 0
    finished_thinking: bool = False  # the last thought said nextThoughtNeeded=false

    def start(self, goal: str) -> None:
        """Begin a new goal. `adk web` reuses one ledger for every message, so everything resets here."""
        self.goal = goal
        self.tool_calls = 0
        self.calls_by_tool = {}
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.model_calls = 0
        self.thoughts = 0
        self.revisions = 0
        self.finished_thinking = False

    def record_tool_call(self, name: str) -> bool:
        """Count a tool call. Returns False when the budget is exhausted."""
        if self.tool_calls >= self.max_tool_calls:
            return False
        self.tool_calls += 1
        self.calls_by_tool[name] = self.calls_by_tool.get(name, 0) + 1
        return True

    def record_thought(self, args: dict) -> None:
        self.thoughts += 1
        if args.get("isRevision"):
            self.revisions += 1
        self.finished_thinking = not args.get("nextThoughtNeeded", True)

    def record_usage(self, prompt: int | None, output: int | None) -> None:
        self.model_calls += 1
        self.prompt_tokens += prompt or 0
        self.output_tokens += output or 0

    def cost_usd(self, prices: dict[str, tuple[float, float]] | None = None) -> float | None:
        price = (prices or DEFAULT_PRICES).get(self.model)
        if os.getenv("GEMINI_PRICE_IN") and os.getenv("GEMINI_PRICE_OUT"):
            price = (float(os.environ["GEMINI_PRICE_IN"]), float(os.environ["GEMINI_PRICE_OUT"]))
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000


def parse_plan(text: str) -> Plan | None:
    try:
        return Plan.model_validate_json(text)
    except ValueError:
        return None


def graph_problems(plan: Plan) -> list[str]:
    """Structural faults: duplicate ids, unknown or self dependencies, cycles, wrong step count."""
    problems: list[str] = []
    ids = [s.id for s in plan.steps]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"duplicate id {dup}")
    known = set(ids)
    for s in plan.steps:
        for d in s.depends_on:
            if d == s.id:
                problems.append(f"{s.id} depends on itself")
            elif d not in known:
                problems.append(f"{s.id} depends on unknown step {d}")
    if not problems and topological_order(plan) is None:
        problems.append("dependency cycle")
    if not MIN_STEPS <= len(plan.steps) <= MAX_STEPS:
        problems.append(f"{len(plan.steps)} steps (expected {MIN_STEPS}-{MAX_STEPS})")
    return problems


def topological_order(plan: Plan) -> list[str] | None:
    """Step ids in an order that respects depends_on, or None if the graph has a cycle."""
    deps = {s.id: set(s.depends_on) for s in plan.steps}
    order: list[str] = []
    while deps:
        ready = sorted(i for i, d in deps.items() if not d)
        if not ready:
            return None
        for i in ready:
            order.append(i)
            del deps[i]
        for d in deps.values():
            d.difference_update(ready)
    return order


def unchecked_steps(plan: Plan) -> list[str]:
    """Steps with no acceptance check (empty or placeholder done_when)."""
    return [s.id for s in plan.steps if len(s.done_when.strip()) < 8 or s.done_when.strip().lower() in {"tbd", "n/a", "done"}]


EFFORT_DAYS = {"S": 0.5, "M": 1.5, "L": 4.0}


def critical_path(plan: Plan) -> tuple[list[str], float]:
    """Longest dependency chain by estimated effort (S=0.5, M=1.5, L=4 days). Assumes a valid graph."""
    steps = {s.id: s for s in plan.steps}
    best: dict[str, tuple[float, list[str]]] = {}
    for sid in topological_order(plan) or []:
        s = steps[sid]
        days = EFFORT_DAYS.get(s.effort.strip().upper()[:1], 1.5)
        prev = max((best[d] for d in s.depends_on), default=(0.0, []), key=lambda b: b[0])
        best[sid] = (prev[0] + days, prev[1] + [sid])
    if not best:
        return [], 0.0
    total, path = max(best.values(), key=lambda b: b[0])
    return path, total


def thinking_problems(ledger: RunLedger) -> list[str]:
    problems = []
    if ledger.thoughts < MIN_THOUGHTS:
        problems.append(f"only {ledger.thoughts} reasoning steps (need {MIN_THOUGHTS})")
    if not ledger.finished_thinking:
        problems.append("reasoning never reached its final thought")
    return problems


def render_markdown(plan: Plan) -> str:
    """The human-readable plan, rendered from the validated structure."""
    order = topological_order(plan) or [s.id for s in plan.steps]
    by_id = {s.id: s for s in plan.steps}
    path, days = critical_path(plan)
    lines = [f"# Plan: {plan.goal}", ""]
    if plan.assumptions:
        lines += ["## Assumptions", *[f"- {a}" for a in plan.assumptions], ""]
    lines += ["## Steps (in dependency order)", "", "| # | Step | Needs | Effort | Done when |", "|---|---|---|---|---|"]
    for sid in order:
        s = by_id[sid]
        lines.append(f"| {s.id} | **{s.title}**: {s.details} | {', '.join(s.depends_on) or '-'} | {s.effort} | {s.done_when} |")
    lines += ["", f"**Critical path:** {' → '.join(path)} (about {days:g} days of effort)", ""]
    if plan.risks:
        lines += ["## Risks", *[f"- {r}" for r in plan.risks], ""]
    if plan.open_questions:
        lines += ["## Open questions", *[f"- {q}" for q in plan.open_questions], ""]
    return "\n".join(lines)
