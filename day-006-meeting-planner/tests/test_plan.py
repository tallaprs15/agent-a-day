import datetime as dt

from meeting_planner.plan import LocalTime, MeetingPlan, Participant, Slot, failed, render_markdown, verify_plan

PEOPLE = [Participant(name="Mel", timezone="Australia/Melbourne"), Participant(name="Mum", timezone="Asia/Kolkata")]


def good_plan(**over) -> MeetingPlan:
    # 2026-11-03 04:00 UTC = 15:00 Melbourne (AEDT, +11) = 09:30 Mumbai (+5:30): Tuesday in both.
    slot = Slot(
        start_utc="2026-11-03 04:00",
        end_utc="2026-11-03 04:45",
        local_times=[
            LocalTime(name="Mel", timezone="Australia/Melbourne", local_start="2026-11-03 15:00", local_end="2026-11-03 15:45"),
            LocalTime(name="Mum", timezone="Asia/Kolkata", local_start="2026-11-03 09:30", local_end="2026-11-03 10:15"),
        ],
        rationale="Inside both working days.",
    )
    data = dict(title="Sync", duration_minutes=45, participants=PEOPLE, slots=[slot])
    data.update(over)
    return MeetingPlan(**data)


def test_correct_plan_passes():
    assert failed(verify_plan(good_plan(), today=dt.date(2026, 10, 23))) == {}


def test_wrong_local_time_is_caught():
    plan = good_plan()
    plan.slots[0].local_times[1].local_start = "2026-11-03 09:00"  # forgot the half-hour offset
    assert "local_times_correct" in failed(verify_plan(plan))


def test_outside_working_hours_is_caught():
    plan = good_plan(working_hours_start="09:00", working_hours_end="15:30")
    assert "within_working_hours" in failed(verify_plan(plan))  # Melbourne ends 15:45


def test_weekend_is_caught():
    slot = Slot(
        start_utc="2026-11-07 04:00",  # Saturday in both zones
        end_utc="2026-11-07 04:45",
        local_times=[
            LocalTime(name="Mel", timezone="Australia/Melbourne", local_start="2026-11-07 15:00", local_end="2026-11-07 15:45"),
            LocalTime(name="Mum", timezone="Asia/Kolkata", local_start="2026-11-07 09:30", local_end="2026-11-07 10:15"),
        ],
        rationale="x",
    )
    assert "weekdays_only" in failed(verify_plan(good_plan(slots=[slot])))


def test_dst_is_respected():
    # 2026-03-31 is still AEDT (+11); Melbourne leaves DST on 2026-04-05. A plan using +10 must fail.
    slot = Slot(
        start_utc="2026-03-31 04:00",
        end_utc="2026-03-31 04:45",
        local_times=[
            LocalTime(name="Mel", timezone="Australia/Melbourne", local_start="2026-03-31 14:00", local_end="2026-03-31 14:45"),
            LocalTime(name="Mum", timezone="Asia/Kolkata", local_start="2026-03-31 09:30", local_end="2026-03-31 10:15"),
        ],
        rationale="x",
    )
    assert "local_times_correct" in failed(verify_plan(good_plan(slots=[slot])))


def test_bad_zone_duration_and_past_are_caught():
    plan = good_plan(duration_minutes=30, participants=[Participant(name="X", timezone="Mars/Olympus")])
    bad = failed(verify_plan(plan, today=dt.date(2027, 1, 1)))
    assert {"timezones_valid", "durations_match", "not_in_past"} <= bad.keys()


def test_empty_plan_needs_a_reason_and_a_question_is_valid():
    assert "has_outcome" in failed(verify_plan(MeetingPlan(participants=PEOPLE)))
    assert failed(verify_plan(MeetingPlan(participants=PEOPLE, no_overlap_reason="Working hours never overlap."))) == {}
    assert failed(verify_plan(MeetingPlan(needs_clarification="Which cities?"))) == {}


def test_render_markdown_lists_each_participant():
    text = render_markdown(good_plan())
    assert "Option 1" in text and "Asia/Kolkata" in text
