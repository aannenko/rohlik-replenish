"""§6.5, §13.2: the delivery date T without `--date` — the earliest date with a free slot in days 0..3, each day read
on its own. Late in the day, day "0" can answer with the tool's usage hint and no `availabilityDays` (§4.5), so a
failed day is skipped rather than ending the search."""

from datetime import timedelta

import pytest

from engine.constants import READ_RETRIES
from engine.rohlik import TransportError
from tests.helpers import Scenario, at, section

MILK, BREAD = 101, 102
NOW = at("2026-09-28", 20, 4)  # a Monday evening, when day 0 can fail (§4.5)
TODAY = NOW.date()
USAGE_HINT = ("All times are in 24-hour format with a 12-hour hint in parentheses. IMPORTANT: always show the "
              "customer the slot's time window")
NO_FREE = "no free delivery slot in the next 4 days"


def household(tmp_path) -> Scenario:
    s = Scenario(tmp_path)
    s.product(MILK, "Milk 1 l", "1 l", 25.0)
    s.product(BREAD, "Bread 500 g", "500 g", 40.0)
    s.history("2026-04-01", 7, 26, {MILK: 2, BREAD: 1})
    s.first_runs(at("2026-09-27", 9), {MILK: ("milk", "cheapest", "l", "chilled"),
                                       BREAD: ("bread", "cheapest", "kg", "bakery")}, date_="2026-09-29")
    return s


def day_of(k: int):
    return TODAY + timedelta(days=k)


def free(k: int) -> dict:
    """A day with a free slot (it has a `slotId`), in the verified shape (§4.5)."""
    return {"data": {"availabilityDays": [{"date": day_of(k).isoformat(), "slots": {"8": [
        {"slotId": 4700 + k, "timeWindow": "08:00-09:00", "slotType": "STANDARD", "capacityPercent": 40,
         "price": 0}]}}]}}


def full(k: int) -> dict:
    """A day whose slots are all full: `capacityPercent: "NO CAPACITY"` and no `slotId`."""
    return {"data": {"availabilityDays": [{"date": day_of(k).isoformat(), "slots": {"8": [
        {"timeWindow": "08:00-09:00", "slotType": "STANDARD", "capacityPercent": "NO CAPACITY", "price": 0}]}}]}}


def fail(s: Scenario, k: int, how: str) -> None:
    """A failed read of day k (§4.4): the live answer (the usage hint as its message, no `availabilityDays`), or an
    `is_error` answer."""
    if how == "hint":
        s.fake.timeslots[str(k)] = {"message": USAGE_HINT}
    else:
        s.fake.failures[f"get_timeslots_checkout:{k}"] = "Something went wrong"


def slot_reads(s: Scenario) -> list[str]:
    return [args["timeslots_day"] for tool, args in s.fake.calls if tool == "get_timeslots_checkout"]


def slot_warnings(env) -> list[str]:
    return [w for w in env.warnings if "slot" in w]


@pytest.mark.parametrize("failing, found", [((0,), 1), ((0, 1), 2), ((1,), 0)])
def test_a_day_whose_read_fails_is_skipped_and_the_search_goes_on(tmp_path, failing, found):
    """§13.2: day 0's read fails and day 1 has a slot → T = day 1, "earliest slot", no warning; also two failed days,
    and a failed day after the one with a slot, which is never read."""
    s = household(tmp_path)
    s.fake.timeslots = {str(k): free(k) for k in range(4)}
    for i, k in enumerate(failing):
        fail(s, k, "hint" if i % 2 == 0 else "is_error")
    env = s.run(NOW, dry=True)
    assert env.status == "dry_run", env.summary_md
    assert (env.data["T"], env.data["T_source"]) == (day_of(found).isoformat(), "earliest slot")
    assert slot_reads(s) == [str(k) for k in range(found + 1)], "each day once, up to the first with a free slot"
    assert slot_warnings(env) == [], "no §6.5 warning"
    md = env.summary_md
    assert md.startswith(f"**Delivery {day_of(found).day} Sep** (earliest slot) · next expected ~"), md[:120]
    assert NO_FREE not in md and "couldn't read the delivery slots" not in md
    # §8.1 section 6: a date the user didn't give is still pointed out
    assert f"- planned for delivery on {day_of(found).day} Sep — re-run with `--date` if you pick another day" in \
        section(md, "Warnings")


def test_every_day_read_and_none_free_plans_for_tomorrow_and_says_so(tmp_path):
    """§13.2: every day read but none has a slot → today + 1 with "no free delivery slot in the next 4 days"."""
    s = household(tmp_path)
    s.fake.timeslots = {"0": full(0), "1": full(1), "2": full(2)}  # day 3: an empty `availabilityDays`
    env = s.run(NOW, dry=True)
    assert env.status == "dry_run", env.summary_md
    assert (env.data["T"], env.data["T_source"]) == (day_of(1).isoformat(), "tomorrow, no slot found")
    assert slot_reads(s) == ["0", "1", "2", "3"]
    assert slot_warnings(env) == [NO_FREE]
    md = env.summary_md
    assert md.startswith("**Delivery 29 Sep** (tomorrow, no slot found) · next expected ~"), md[:120]
    assert "((" not in md.splitlines()[0] and "))" not in md.splitlines()[0], "no nested brackets in the header"
    assert f"- {NO_FREE}" in section(md, "Warnings")
    assert "couldn't read the delivery slots" not in md


@pytest.mark.parametrize("failing, n", [((0, 2), 2), ((0, 1, 2, 3), 4)])
def test_some_days_unreadable_and_none_free_says_how_many_couldnt_be_read(tmp_path, failing, n):
    """§13.2: some reads fail and none has a slot → today + 1 with "couldn't read the delivery slots for N of the
    next 4 days": not "no free slot", which nobody checked for the failed days."""
    s = household(tmp_path)
    s.fake.timeslots = {str(k): full(k) for k in range(4)}
    for i, k in enumerate(failing):
        fail(s, k, "hint" if i % 2 == 0 else "is_error")
    env = s.run(NOW, dry=True)
    assert env.status == "dry_run", env.summary_md
    assert (env.data["T"], env.data["T_source"]) == (day_of(1).isoformat(), "tomorrow, no slot found")
    assert slot_reads(s) == ["0", "1", "2", "3"], "a failed day doesn't end the search"
    warning = f"couldn't read the delivery slots for {n} of the next 4 days"
    assert slot_warnings(env) == [warning]
    assert f"- {warning}" in section(env.summary_md, "Warnings") and NO_FREE not in env.summary_md


def test_a_transport_failure_that_outlasts_its_retries_ends_the_search(tmp_path):
    """§13.2: a transport failure on day 0 → no further day is read (days 1..3 had free slots). The run still
    plans, for tomorrow, and counts the days it couldn't read."""
    s = household(tmp_path)
    s.fake.timeslots = {str(k): free(k) for k in range(4)}

    def drop_day_0(tool, args):
        if tool == "get_timeslots_checkout" and args.get("timeslots_day") == "0":
            raise TransportError("get_timeslots_checkout: connection reset", retryable=True)

    s.fake.before_call = drop_day_0
    env = s.run(NOW, dry=True)
    assert env.status == "dry_run", env.summary_md
    assert slot_reads(s) == ["0"] * (1 + READ_RETRIES), "day 0 with its retries, then no other day"
    assert (env.data["T"], env.data["T_source"]) == (day_of(1).isoformat(), "tomorrow, no slot found")
    assert slot_warnings(env) == ["couldn't read the delivery slots for 4 of the next 4 days"]
    assert env.data["plan"], "the run still plans"


def test_a_transport_failure_after_full_days_counts_only_the_days_left(tmp_path):
    s = household(tmp_path)
    s.fake.timeslots = {"0": full(0), "1": full(1), "2": free(2), "3": free(3)}
    s.fake.transport_failures.add("get_timeslots_checkout:2")
    env = s.run(NOW, dry=True)
    assert slot_reads(s) == ["0", "1", "2"]
    assert slot_warnings(env) == ["couldn't read the delivery slots for 2 of the next 4 days"]


def test_a_date_the_user_gives_reads_no_slots(tmp_path):
    s = household(tmp_path)
    env = s.run(NOW, date_="2026-10-01", dry=True)
    assert (env.data["T"], env.data["T_source"]) == ("2026-10-01", "your date")
    assert slot_reads(s) == [] and slot_warnings(env) == []
    assert "planned for delivery on" not in env.summary_md
