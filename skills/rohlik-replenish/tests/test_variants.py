"""§13.1 variants, sale pools, caps, PREORDER and missing live data."""

from datetime import date, datetime, timedelta, timezone

import pytest

from engine.choice import Allocator, Cand, Live, cheapest_order, parse_live, qualifying_head
from tests.helpers import Scenario, at, section

E = date(2026, 9, 28)


def live(sku, original=10.0, sale=None, until=None, status="AVAILABLE", max_available=50, expected=None, text=None):
    d = {"name": f"P{sku}", "textualAmount": text,
         "prices": {"originalPrice": original, "salePrice": sale, "saleValidTill": until},
         "stock": {"availabilityStatus": status, "maxAvailableAmount": max_available}}
    if expected:
        d["stock"]["expectedReplenishment"] = expected
    return parse_live(sku, d, E, 0.35)


def cand(sku, price, size=1.0, discount=0.0, qualifies=False, room=None, last=None, lv=None):
    lv = lv or live(sku, original=price * size)
    return Cand(sku, size, price * size, price, discount, qualifies, room,
                datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(days=last) if last is not None else None, lv)


# ------------------------------------------------------------------------------ live data rules
def test_sale_counts_discount_and_qualification():
    assert live(1, 10, 6.5, "2026-10-01").qualifies  # 35 %
    assert not live(1, 10, 6.51, "2026-10-01").qualifies  # 34.9 %
    assert not live(1, 10, 5, "2026-09-28").sale_counts  # ends today
    assert live(1, 10, 9, None).sale_counts and live(1, 10, 9, None).discount == pytest.approx(0.1)
    unknown = live(1, None, 5, "2026-10-01")
    assert unknown.sale_counts and unknown.discount is None and not unknown.qualifies
    assert live(1, 10).discount is None  # no sale marker -> none


def test_preorder_before_and_after_t():
    pre = live(1, status="PREORDER", expected="2026-10-04T06:00:00+02:00")
    assert pre.orderable(date(2026, 10, 4)) and not pre.orderable(date(2026, 10, 3))
    assert not live(1, status="UNAVAILABLE").orderable(date(2026, 10, 4))
    assert not live(1, max_available=0).orderable(date(2026, 10, 4))


# ------------------------------------------------------------------------------ orders and pools
def test_cheapest_ties_to_larger_discount_then_most_recent():
    a = cand(1, 10.0, discount=0.0, last=5)
    b = cand(2, 10.0, discount=0.2, last=1)
    c = cand(3, 10.0, discount=0.0, last=9)
    d_ = cand(4, 9.0)
    assert [x.sku for x in cheapest_order([a, b, c, d_], True)] == [4, 2, 3, 1]
    assert [x.sku for x in cheapest_order([a, b, c, d_], False)] == [4, 2, 1, 3]  # assorted: least recent


def test_sale_pool_is_the_qualifying_head_of_the_cheapest_order():
    cheap_regular = cand(1, 8.0)
    pricey_on_sale = cand(2, 9.0, discount=0.4, qualifies=True)
    assert qualifying_head(cheapest_order([cheap_regular, pricey_on_sale], True)) == []
    cheaper_on_sale = cand(3, 7.0, discount=0.4, qualifies=True)
    head = qualifying_head(cheapest_order([cheap_regular, pricey_on_sale, cheaper_on_sale], True))
    assert [c.sku for c in head] == [3]


# ------------------------------------------------------------------------------ allocation
def test_assorted_spreads_to_the_n_cheapest_before_any_second_unit():
    pool = cheapest_order([cand(i, 10.0 + i) for i in range(1, 6)], False)
    a = Allocator(pool, "spread_equal")
    a.fill_normal(3.0)
    assert sorted(a.alloc.normal) == [1, 2, 3]
    a.fill_normal(7.0)
    assert sorted(a.alloc.normal.values()) == [1, 1, 1, 2, 2]


def test_assorted_counts_user_owned_cart_units():
    pool = cheapest_order([cand(1, 10.0), cand(2, 11.0)], False)
    a = Allocator(pool, "spread_equal", user_units={1: 1})
    a.fill_normal(1.0)
    assert a.alloc.normal == {2: 1}


def test_cart_cap_moves_spread_on_and_stops_single():
    pool = cheapest_order([cand(1, 10.0, room=1), cand(2, 11.0)], False)
    a = Allocator(pool, "spread_equal")
    a.fill_normal(3.0)
    assert a.alloc.normal == {1: 1, 2: 2}
    single = Allocator(cheapest_order([cand(1, 10.0, room=1), cand(2, 11.0)], True), "single")
    single.fill_normal(3.0)
    assert single.alloc.normal == {1: 1} and single.alloc.capped_basket
    none_fit = Allocator([cand(1, 10.0, room=0)], "single")
    none_fit.fill_normal(1.0)
    assert none_fit.alloc.total_units == 0 and none_fit.alloc.capped_basket


def test_cap_cuts_the_stockup_part_first():
    a = Allocator([cand(1, 10.0, room=3)], "single")
    a.fill_normal(2.0)
    a.fill_stockup(held=0.0, max_level=6.0)
    assert a.alloc.normal == {1: 2} and a.alloc.stockup == {1: 1} and a.alloc.capped_basket


def test_stockup_rounds_up_but_never_takes_the_order_above_max():
    a = Allocator([cand(1, 10.0, size=0.5)], "single")
    a.fill_normal(0.0)
    a.fill_stockup(held=0.3, max_level=2.0)  # needs 1.7 -> 4 packs (2.0), not 5
    assert a.alloc.stockup == {1: 4}
    b = Allocator([cand(1, 10.0, size=0.75)], "single")
    b.fill_stockup(held=0.0, max_level=2.0)  # 3 packs would be 2.25 > MAX: stops at 2
    assert b.alloc.stockup == {1: 2}


def test_need_above_max_is_covered_in_full():
    a = Allocator([cand(1, 10.0, size=1.0)], "single")
    a.fill_normal(2.4)
    a.fill_stockup(held=0.0, max_level=2.0)
    assert a.alloc.normal == {1: 3} and a.alloc.stockup == {}


def test_a_unit_that_does_not_fit_stops_allocation_without_skipping_to_a_smaller_pack():
    big, small = cand(1, 5.0, size=1.0), cand(2, 6.0, size=0.2)
    a = Allocator([big, small], "single")
    a.fill_stockup(held=0.0, max_level=1.5)
    assert a.alloc.stockup == {1: 1}


def test_always_adds_units_toward_the_usual_batch_with_ties_added():
    a = Allocator([cand(1, 10.0, size=1.0)], "single")
    a.fill_normal(0.4, toward=2.5)
    assert a.alloc.normal == {1: 3}  # round half up toward the usual batch
    b = Allocator([cand(1, 10.0, size=1.0)], "single")
    b.fill_normal(0.4, toward=2.4)
    assert b.alloc.normal == {1: 2}


def test_short_life_p90_cap_still_buys_at_least_one():
    a = Allocator([cand(1, 10.0, size=1.0)], "single")
    a.fill_normal(5.0, cap=2.0)
    assert a.alloc.normal == {1: 2} and a.alloc.capped_usual
    b = Allocator([cand(1, 10.0, size=1.0)], "single")
    b.fill_normal(5.0, cap=0.3)
    assert b.alloc.normal == {1: 1}


def _variety_rotation(weights: dict[int, float], sizes: dict[int, float], runs: int) -> dict[int, int]:
    history: list[tuple[int, float]] = []
    counts = {s: 0 for s in weights}
    for _ in range(runs):
        rotation = {s: 0.0 for s in weights}
        for s, q in history[-6:]:
            rotation[s] += q
        pool = [cand(s, 10.0, size=sizes[s]) for s in sorted(weights)]
        a = Allocator(pool, "spread_mix", weights=weights, rotation=rotation)
        a.fill_normal(min(sizes.values()) * 0.5)  # one unit
        (s, _), = a.alloc.sequence
        counts[s] += 1
        history.append((s, sizes[s]))
    return counts


def test_variety_with_fixed_weights_buys_every_type_near_its_weight():
    counts = _variety_rotation({1: 3.0, 2: 2.0, 3: 1.0}, {1: 1.0, 2: 1.0, 3: 1.0}, 60)
    total = sum(counts.values())
    for s, w in {1: 3 / 6, 2: 2 / 6, 3: 1 / 6}.items():
        assert counts[s] > 0
        assert abs(counts[s] / total - w) <= 0.10, counts


def test_variety_with_uneven_packs_still_buys_every_type():
    counts = _variety_rotation({1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}, {1: 0.3, 2: 0.15, 3: 0.4, 4: 0.2}, 60)
    assert all(v > 0 for v in counts.values()), counts


# ------------------------------------------------------------------------------ end to end
MOZZ, GOUDA, PROVO = 301, 302, 303
MADETA, PILOS = 401, 402


def cheese_household(tmp_path):
    s = Scenario(tmp_path)
    s.product(MOZZ, "Mozzarella 40+ slices", "100 g", 30.0)
    s.product(GOUDA, "Gouda 48+ slices", "100 g", 32.0)
    s.product(PROVO, "Provolone slices", "100 g", 35.0)
    s.product(MADETA, "Madeta butter 250 g", "250 g", 55.0)
    s.product(PILOS, "Pilos butter 250 g", "250 g", 45.0)
    day = date(2026, 4, 1)
    for i in range(26):
        lines = {MOZZ: 2, GOUDA: 1, MADETA: 1}
        if i % 2 == 0:
            lines[PROVO] = 1
        if i == 20:
            lines = {MOZZ: 2, GOUDA: 1, PILOS: 1}  # a one-off purchase doesn't flip solo's usual
        s.delivered(day.isoformat(), lines)
        day += timedelta(days=7)
    mapping = {MOZZ: ("sliced cheese", "variety", "kg", "chilled"), GOUDA: ("sliced cheese", "variety", "kg", "chilled"),
               PROVO: ("sliced cheese", "variety", "kg", "chilled"),
               MADETA: ("butter", "solo", "kg", "chilled", {"preferred": True}), PILOS: ("butter", "solo", "kg", "chilled")}
    s.first_runs(at("2026-09-28", 9), mapping, date_="2026-09-30")
    return s


def test_solo_buys_its_preferred_product_and_falls_back_for_the_need_only(tmp_path):
    s = cheese_household(tmp_path)
    env = s.run(at("2026-09-28", 10), date_="2026-09-30", dry=True)
    butter = [r for r in env.data["plan"] if r["tag"] == "butter"]
    assert [r["sku"] for r in butter] == [MADETA], "a one-off purchase and a cheaper price don't flip solo"
    assert butter[0]["fallback_from"] is None
    s.set_price(MADETA, status="UNAVAILABLE")
    s.fake.products[MADETA]["stock"]["availabilityReason"] = "Vyprodáno."
    env = s.run(at("2026-09-28", 11), date_="2026-09-30", dry=True)
    butter = [r for r in env.data["plan"] if r["tag"] == "butter"]
    assert [r["sku"] for r in butter] == [PILOS] and butter[0]["fallback_from"] == MADETA
    assert "Madeta butter 250 g unavailable — Pilos butter 250 g instead" in butter[0]["why"], "data keeps the why"
    # §8.1 section 4, §13.2: the reminder sits under "Reminders", not under a "Paused" heading
    assert ("- butter: Madeta butter 250 g unavailable (Vyprodáno.) — Pilos butter 250 g instead; name another to "
            "change it") in section(env.summary_md, "Reminders"), "the reminder names the preferred product and why"
    assert "### Paused" not in env.summary_md, "nothing is paused"


def test_variety_skips_a_sold_out_type_and_reports_the_mix(tmp_path):
    s = cheese_household(tmp_path)
    s.set_price(GOUDA, status="UNAVAILABLE")
    env = s.run(at("2026-09-28", 10), date_="2026-09-30", dry=True)
    cheese = [r for r in env.data["plan"] if r["tag"] == "sliced cheese"]
    assert cheese and GOUDA not in {r["sku"] for r in cheese}
    assert "your recent mix" in cheese[0]["why"]


def test_no_live_data_and_no_candidate_is_unavailable(tmp_path):
    s = cheese_household(tmp_path)
    for sku in (MADETA, PILOS):
        del s.fake.products[sku]
    env = s.run(at("2026-09-28", 10), date_="2026-09-30", dry=True)
    butter = [r for r in env.data["plan"] if r["tag"] == "butter"]
    assert butter and butter[0]["reason"] == "unavailable"
    assert "no live data" in env.summary_md


def test_a_dropped_type_is_a_reminder_for_three_finished_runs_then_only_in_status(tmp_path):
    """§2 Reminder, §6.11 rule 11: from the day the type was dropped, every run shows it (dry runs too) until 3
    non-dry runs have finished normally; then only `status` lists it."""
    from engine.status import status

    s = cheese_household(tmp_path)
    text = "Provolone slices dropped from sliced cheese"
    now, removed, env = at("2026-09-28", 10), 0, None
    for _ in range(12):  # weekly: the user removes the provolone whenever the agent offers it
        t = (now + timedelta(days=2)).date().isoformat()
        s.run(now, date_=t)
        if s.cart().get(PROVO):
            s.fake.set_cart_qty(PROVO, 0)
            removed += 1
        oid = s.checkout(now + timedelta(minutes=30), t)
        env = s.run(now + timedelta(hours=3), date_=t)  # sees the order: the second removal drops the type
        s.deliver(oid)
        if removed == 2:
            break
        now += timedelta(days=7)
    assert removed == 2
    shown = [text in env.summary_md]  # the first run that knows: dated the day that order was placed
    later = now + timedelta(days=7)
    t = (later + timedelta(days=2)).date().isoformat()
    for i, dry in enumerate((True, False, False, False)):
        shown.append(text in s.run(later + timedelta(minutes=10 * i), date_=t, dry=dry).summary_md)
    assert shown == [True, True, True, True, False], "3 finished non-dry runs; the dry run doesn't count"
    md, data = status(s.conn, later + timedelta(hours=1))
    assert data["dropped"] == ["Provolone slices (sliced cheese)"]


# ------------------------------------------------------------------------------ fit (§6.13)
def test_fit_drops_packs_over_one_and_a_half_times_the_limit_while_another_fits():
    from engine.planner import _fit

    six, one, three, five = cand(6, 25.0, size=6.0), cand(1, 30.0), cand(3, 27.0, size=3.0), cand(5, 20.0, size=5.0)
    assert set(_fit({6: six, 1: one}, 2.0)) == {1}, "a 6 l pack against a P90 of 2 l"
    assert set(_fit({6: six, 1: one, 3: three}, 2.0)) == {1, 3}, "3 l is 1.5 x 2 l: it stays a candidate"
    assert set(_fit({6: six, 5: five}, 2.0)) == {5}, "none fits: only the smallest pack stays"
    assert set(_fit({6: six, 1: one}, None)) == {1, 6}, "no limit: nothing is dropped"


MILK1, MILK6 = 601, 606


def litre_household(tmp_path, policy):
    """2 l a week in 1 l packs, except two 6 l packs (25 CZK/l against 30) among the last three orders; then a
    month without milk. P90 stays 2 l, and the 6 l pack is `solo`'s preferred product."""
    s = Scenario(tmp_path)
    s.product(MILK1, "Milk 1 l", "1 l", 30.0)
    s.product(MILK6, "Milk 6 l pack", "6 l", 150.0)
    day = date(2026, 4, 1)
    for i in range(23):
        s.delivered(day.isoformat(), {MILK6: 1} if i in (20, 21) else {MILK1: 2})
        day += timedelta(days=7)
    mapping = {MILK1: ("milk", policy, "l", "chilled"),
               MILK6: ("milk", policy, "l", "chilled", {"preferred": True} if policy == "solo" else {})}
    s.first_runs(at("2026-10-05", 9), mapping, date_="2026-10-07")
    return s


@pytest.mark.parametrize("policy", ["cheapest", "solo"])
def test_fit_buys_1_l_packs_rather_than_a_6_l_pack_against_a_p90_of_2_l(tmp_path, policy):
    s = litre_household(tmp_path, policy)
    env = s.run(at("2026-10-05", 10), date_="2026-10-07", dry=True)
    milk = [r for r in env.data["plan"] if r["tag"] == "milk"]
    assert milk and {r["sku"] for r in milk} == {MILK1}, s.dump(env)
    if policy == "solo":  # the fit rule drops the preferred product, so solo falls back to the cheapest that fits
        assert "Milk 6 l pack comes in a bigger pack than needed — Milk 1 l instead" in milk[0]["why"]
        assert milk[0]["fallback_from"] == MILK6
        assert ("milk: Milk 6 l pack comes in a bigger pack than needed — Milk 1 l instead; name another to change "
                "it") in env.summary_md
