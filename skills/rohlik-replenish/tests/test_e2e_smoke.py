"""End-to-end: needs_tags -> tags apply -> first (dry) run -> writing run -> idempotent re-runs (§13.6 shape)."""

from tests.helpers import Scenario, at


def _milk_household(tmp_path):
    s = Scenario(tmp_path)
    s.product(101, "Milk 1.5% 1 l", "1 l", 25.0)
    s.product(102, "Bread 500 g", "500 g", 40.0)
    s.product(103, "Butter 250 g", "250 g", 55.0)
    s.history("2026-04-01", 7, 26, {101: 2, 102: 1})
    s.history("2026-04-03", 14, 13, {103: 1})
    return s


MAP = {101: ("milk 1.5%", "cheapest", "l", "chilled"), 102: ("bread", "cheapest", "kg", "bakery"),
       103: ("butter", "cheapest", "kg", "chilled")}


def test_first_run_is_dry_then_writes_then_idempotent(tmp_path):
    s = _milk_household(tmp_path)
    now = at("2026-09-28", 9)
    first = s.first_runs(now, MAP, date_="2026-09-30")
    assert first.data["first_run"] is True
    assert first.data["dry_run"] is True
    assert not s.fake.writes, "a first run never writes"
    assert "## Setup" in first.summary_md

    second = s.run(at("2026-09-28", 10), date_="2026-09-30")
    assert second.status == "ok", second.summary_md
    assert s.fake.writes, s.dump(second)
    cart = s.cart()
    assert cart.get(101), cart

    writes_before = len(s.fake.writes)
    third = s.run(at("2026-09-28", 11), date_="2026-09-30")
    assert third.status == "ok", third.summary_md
    assert len(s.fake.writes) == writes_before, "an immediate re-run makes zero writes"
    assert s.cart() == cart
