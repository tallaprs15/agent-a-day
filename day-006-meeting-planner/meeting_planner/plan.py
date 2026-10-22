"""The typed plan the agent must return, and a deterministic verifier for it.

The model fills in a MeetingPlan (enforced by ADK's `output_schema`). `verify_plan` then recomputes every
local time with the standard library's tz database, so a plan with a wrong conversion, a slot outside
working hours or a weekend meeting fails the run no matter how confident the model sounded.
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

STAMP = "%Y-%m-%d %H:%M"
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class Participant(BaseModel):
    name: str = Field(description="Person or city label as the user gave it")
    timezone: str = Field(description="IANA time zone name, e.g. Australia/Melbourne")


class LocalTime(BaseModel):
    name: str
    timezone: str = Field(description="IANA time zone name")
    local_start: str = Field(description="Start in this person's local time, format YYYY-MM-DD HH:MM")
    local_end: str = Field(description="End in this person's local time, format YYYY-MM-DD HH:MM")


class Slot(BaseModel):
    start_utc: str = Field(description="Start in UTC, format YYYY-MM-DD HH:MM")
    end_utc: str = Field(description="End in UTC, format YYYY-MM-DD HH:MM")
    local_times: list[LocalTime] = Field(description="One entry per participant")
    rationale: str = Field(description="One sentence on why this slot is good or what it costs someone")


class MeetingPlan(BaseModel):
    needs_clarification: str | None = Field(
        default=None,
        description="Set to a question for the user ONLY when the request lacks participants, "
        "time zones or a date range; leave everything else empty in that case",
    )
    title: str = Field(default="", description="Short meeting title")
    duration_minutes: int = Field(default=0, description="Meeting length in minutes")
    working_hours_start: str = Field(default="09:00", description="Local working-day start, HH:MM")
    working_hours_end: str = Field(default="17:00", description="Local working-day end, HH:MM")
    participants: list[Participant] = Field(default_factory=list)
    slots: list[Slot] = Field(default_factory=list, description="Best first, at most 3. Empty if there is no overlap")
    no_overlap_reason: str | None = Field(default=None, description="Why no slot exists, when slots is empty")


def _zone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def _parse(stamp: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(stamp, STAMP)
    except (TypeError, ValueError):
        return None


def verify_plan(plan: MeetingPlan, today: dt.date | None = None) -> dict[str, list[str]]:
    """Return {check_name: [problems]} (an empty list means the check passed)."""
    problems: dict[str, list[str]] = {
        "timezones_valid": [],
        "working_hours_valid": [],
        "durations_match": [],
        "local_times_correct": [],
        "within_working_hours": [],
        "weekdays_only": [],
        "not_in_past": [],
        "slots_distinct": [],
        "has_outcome": [],
    }
    if plan.needs_clarification:
        return problems  # asking a question is a valid outcome; there is nothing else to verify
    if not plan.slots and not plan.no_overlap_reason:
        problems["has_outcome"].append("no slots and no no_overlap_reason")
    if not plan.participants:
        problems["has_outcome"].append("no participants")
    if len(plan.slots) > 3:
        problems["has_outcome"].append(f"{len(plan.slots)} slots (max 3)")

    for p in plan.participants:
        if _zone(p.timezone) is None:
            problems["timezones_valid"].append(p.timezone)
    if not (HHMM.match(plan.working_hours_start) and HHMM.match(plan.working_hours_end)):
        problems["working_hours_valid"].append(f"{plan.working_hours_start}-{plan.working_hours_end}")
        return problems
    wh_start = dt.datetime.strptime(plan.working_hours_start, "%H:%M").time()
    wh_end = dt.datetime.strptime(plan.working_hours_end, "%H:%M").time()
    if wh_start >= wh_end:
        problems["working_hours_valid"].append("working hours must start before they end")
        return problems

    wanted = {p.name: p.timezone for p in plan.participants}
    starts: list[str] = []
    for i, slot in enumerate(plan.slots, 1):
        label = f"slot {i}"
        start, end = _parse(slot.start_utc), _parse(slot.end_utc)
        if start is None or end is None:
            problems["durations_match"].append(f"{label}: bad UTC timestamp")
            continue
        starts.append(slot.start_utc)
        if int((end - start).total_seconds() // 60) != plan.duration_minutes:
            problems["durations_match"].append(f"{label}: {start}-{end} is not {plan.duration_minutes} min")
        if today and start.date() < today:
            problems["not_in_past"].append(f"{label}: {slot.start_utc}")
        listed = {lt.name for lt in slot.local_times}
        for name in wanted.keys() - listed:
            problems["local_times_correct"].append(f"{label}: missing {name}")
        for lt in slot.local_times:
            zone = _zone(lt.timezone)
            if zone is None:
                problems["timezones_valid"].append(lt.timezone)
                continue
            if wanted.get(lt.name) not in (None, lt.timezone):
                problems["local_times_correct"].append(f"{label}: {lt.name} is {wanted[lt.name]}, not {lt.timezone}")
            local_start = start.replace(tzinfo=ZoneInfo("UTC")).astimezone(zone)
            local_end = end.replace(tzinfo=ZoneInfo("UTC")).astimezone(zone)
            if (lt.local_start, lt.local_end) != (local_start.strftime(STAMP), local_end.strftime(STAMP)):
                problems["local_times_correct"].append(
                    f"{label}: {lt.name} should be {local_start.strftime(STAMP)}-{local_end.strftime(STAMP)}, "
                    f"plan says {lt.local_start}-{lt.local_end}"
                )
            if local_start.weekday() >= 5 or local_end.weekday() >= 5:
                problems["weekdays_only"].append(f"{label}: {lt.name} on {local_start.strftime('%A')}")
            same_day = local_start.date() == local_end.date()
            if not (same_day and wh_start <= local_start.time() and local_end.time() <= wh_end):
                problems["within_working_hours"].append(
                    f"{label}: {lt.name} {local_start.strftime('%a %H:%M')}-{local_end.strftime('%H:%M')}"
                )
    if len(starts) != len(set(starts)):
        problems["slots_distinct"].append("duplicate start times")
    return problems


def failed(problems: dict[str, list[str]]) -> dict[str, list[str]]:
    return {k: v for k, v in problems.items() if v}


def render_markdown(plan: MeetingPlan) -> str:
    """A human-readable view of the plan (the JSON stays the source of truth)."""
    if plan.needs_clarification:
        return f"**Question:** {plan.needs_clarification}\n"
    lines = [f"# {plan.title or 'Meeting plan'}", ""]
    lines.append(
        f"{plan.duration_minutes} minutes · working hours {plan.working_hours_start}-{plan.working_hours_end} local · "
        + ", ".join(f"{p.name} ({p.timezone})" for p in plan.participants)
    )
    lines.append("")
    if not plan.slots:
        lines += ["No slot works.", "", plan.no_overlap_reason or ""]
    for i, slot in enumerate(plan.slots, 1):
        lines += [f"## Option {i}: {slot.start_utc} UTC", "", slot.rationale, "", "| Who | Local start | Local end |", "|---|---|---|"]
        lines += [f"| {lt.name} ({lt.timezone}) | {lt.local_start} | {lt.local_end} |" for lt in slot.local_times]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
