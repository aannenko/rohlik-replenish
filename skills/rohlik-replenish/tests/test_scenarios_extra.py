"""§13.2 remaining fixture scenarios and §13.1 ownership-at-close cases."""

from datetime import timedelta

from engine.cycles import build_placed, close_ownership, resolve_cycles
from engine.planner import build_events
from engine.world import RunRecord, load_world
from tests.helpers import Scenario, at, d

PASTA = [601, 602, 603, 604]
BREAD, A, B, FILL = 701, 801, 802, 900


def _state(s: Scenario, now, e):
    world = load_world(s.conn)
    cur = RunRecord("r999999-x", 999999, now, None, "current", False, None, None, {})
    state = resolve_cycles(world, build_placed(world, [], cur, e))
    close_ownership(world, state)
    return world, state


def test_assorted_with_a_deep_sale_on_two_skus(tmp_path):
    s = Scenario(tmp_path)
    for i, sku in enumerate(PASTA):
        s.product(sku, f"Pasta {i} 500 g", "500 g", 40.0 + i)
    s.product(FILL, "Rolls", "6 ks", 30.0)
    day = at("2025-11-03").date()
    for i in range(40):  # four kinds bought a few at a time
        s.delivered(day.isoformat(), {PASTA[i % 4]: 1, PASTA[(i + 1) % 4]: 1, FILL: 1})
        day += timedelta(days=7)
    mapping = {sku: ("pasta dry", "assorted", "kg", "dry", {"stockup_mode": "on_sale"}) for sku in PASTA}
    mapping[FILL] = ("rolls", "cheapest", "pcs", "bakery")
    s.first_runs(at("2026-08-10", 9), mapping, date_="2026-08-12")
    s.set_price(602, sale=24.0, sale_until="2026-08-20")
    s.set_price(604, sale=25.0, sale_until="2026-08-20")
    env = s.run(at("2026-08-10", 10), date_="2026-08-12", dry=True)
    rows = [r for r in env.data["plan"] if r["tag"] == "pasta dry"]
    assert rows, s.dump(env)
    assert {r["sku"] for r in rows} <= {602, 604}, "everything an on_sale tag buys is on a qualifying sale"
    if sum(r["normal_units"] + r["stockup_units"] for r in rows) >= 2:
        assert {r["sku"] for r in rows} == {602, 604}, "spread across the qualifying pool"


def test_preorder_bread_for_a_sunday_delivery(tmp_path):
    s = Scenario(tmp_path)
    s.product(BREAD, "Sourdough bread", "500 g", 70.0, status="PREORDER",
              expected="2026-08-16T06:00:00+02:00")  # baked for Sunday
    s.product(FILL, "Rolls", "6 ks", 30.0)
    s.history("2026-03-01", 7, 24, {BREAD: 1, FILL: 1})
    s.first_runs(at("2026-08-13", 9), {BREAD: ("bread sourdough", "cheapest", "kg", "bakery"),
                                       FILL: ("rolls", "cheapest", "pcs", "bakery")}, date_="2026-08-16")
    sunday = s.run(at("2026-08-13", 10), date_="2026-08-16", dry=True)
    assert any(r["sku"] == BREAD and r["reason"] == "due" for r in sunday.data["plan"]), s.dump(sunday)
    saturday = s.run(at("2026-08-13", 11), date_="2026-08-15", dry=True)
    assert any(r["sku"] == BREAD and r["reason"] == "unavailable" for r in saturday.data["plan"])


def test_missing_live_price_is_not_a_candidate(tmp_path):
    s = Scenario(tmp_path)
    s.product(A, "Butter A 250 g", "250 g", 50.0)
    s.product(B, "Butter B 250 g", "250 g", 45.0)
    s.product(FILL, "Rolls", "6 ks", 30.0)
    s.history("2026-03-02", 7, 26, {A: 1, B: 1, FILL: 1})
    s.first_runs(at("2026-09-01", 9), {A: ("butter", "cheapest", "kg", "chilled"),
                                       B: ("butter", "cheapest", "kg", "chilled"),
                                       FILL: ("rolls", "cheapest", "pcs", "bakery")}, date_="2026-09-03")
    s.set_price(B, original=None)  # B, the cheaper one, has no price today
    env = s.run(at("2026-09-01", 10), date_="2026-09-03", dry=True)
    butter = [r for r in env.data["plan"] if r["tag"] == "butter"]
    assert [r["sku"] for r in butter] == [A]


def _two_sku_household(tmp_path):
    s = Scenario(tmp_path)
    s.product(A, "Yogurt A 150 g", "150 g", 15.0)
    s.product(B, "Yogurt B 150 g", "150 g", 16.0)
    s.product(FILL, "Rolls", "6 ks", 30.0)
    s.history("2026-03-02", 7, 26, {A: 2, FILL: 1})
    s.delivered("2026-03-04", {B: 1})  # B was bought once, so it is a candidate
    s.first_runs(at("2026-09-01", 9), {A: ("yogurt", "cheapest", "kg", "chilled"),
                                       B: ("yogurt", "cheapest", "kg", "chilled"),
                                       FILL: ("rolls", "cheapest", "pcs", "bakery")}, date_="2026-09-03")
    return s


def test_switch_then_user_addition_is_user_initiated(tmp_path):
    """R1 adds 2 A, R2 switches to 2 B, the user adds 1 A -> user-initiated."""
    s = _two_sku_household(tmp_path)
    now = at("2026-09-01", 10)
    s.run(now, date_="2026-09-03")
    assert s.cart().get(A) == 2
    s.set_price(A, original=20.0)  # B is now the cheapest
    s.run(now + timedelta(minutes=10), date_="2026-09-03")
    assert s.cart().get(B) == 2 and A not in s.cart()
    s.fake.set_cart_qty(A, 1)  # the user's own unit
    oid = s.checkout(now + timedelta(minutes=30), "2026-09-03")
    world, state = _state(s, now + timedelta(hours=2), d("2026-09-01"))
    p = state.by_order.get(oid)
    if p is None:  # first sighting happens in the next run
        s.run(now + timedelta(hours=1), date_="2026-09-03", dry=True)
        world, state = _state(s, now + timedelta(hours=2), d("2026-09-01"))
        p = state.by_order[oid]
    assert p.cycle is not None
    ev = next(e for e in build_events(world, state, d("2026-09-01"), {})[world.tag_of(A).tag_id]
              if e.order_id == oid)
    assert ev.user, "the user's extra A makes the event user-initiated"


def test_reduced_agent_line_stays_agent_only(tmp_path):
    s = _two_sku_household(tmp_path)
    now = at("2026-09-01", 10)
    s.run(now, date_="2026-09-03")
    s.fake.set_cart_qty(A, 1)  # reduced from 2
    oid = s.checkout(now + timedelta(minutes=30), "2026-09-03")
    s.run(now + timedelta(hours=1), date_="2026-09-03", dry=True)
    world, state = _state(s, now + timedelta(hours=2), d("2026-09-01"))
    ev = next(e for e in build_events(world, state, d("2026-09-01"), {})[world.tag_of(A).tag_id]
              if e.order_id == oid)
    assert not ev.user


def test_units_the_user_adds_to_a_top_up_count_as_taken_stockup(tmp_path):
    s = Scenario(tmp_path)
    s.product(A, "Coffee 250 g", "250 g", 150.0)
    s.product(FILL, "Rolls", "6 ks", 30.0)
    for i, q in enumerate([1, 4, 1, 1, 1, 4, 1, 1, 1, 1, 1, 1, 1]):
        s.delivered((at("2026-03-02") + timedelta(days=14 * i)).date().isoformat(), {A: q, FILL: 1})
    s.history("2026-03-09", 14, 13, {FILL: 1})
    s.first_runs(at("2026-09-21", 9), {A: ("coffee", "cheapest", "kg", "dry", {"stockup_mode": "on_sale"}),
                                       FILL: ("rolls", "cheapest", "pcs", "bakery")}, date_="2026-09-23")
    s.set_price(A, sale=90.0, sale_until="2026-09-30")
    now = at("2026-09-21", 10)
    env = s.run(now, date_="2026-09-23")
    row = next(r for r in env.data["plan"] if r["sku"] == A)
    assert row["stockup_units"] > 0
    written = s.cart()[A]
    s.fake.set_cart_qty(A, written + 2)  # the user raises the top-up
    oid = s.checkout(now + timedelta(minutes=30), "2026-09-23")
    s.run(now + timedelta(hours=1), date_="2026-09-23", dry=True)
    world, state = _state(s, now + timedelta(hours=2), d("2026-09-21"))
    p = state.by_order[oid]
    assert p.cycle.taken_stockup(A, p.qty(A)) == written + 2 - row["normal_units"]
    ev = next(e for e in build_events(world, state, d("2026-09-21"), {})[world.tag_of(A).tag_id]
              if e.order_id == oid)
    assert abs(ev.stockup_base - (written + 2 - row["normal_units"]) * 0.25) < 1e-9


def test_editing_one_variety_type_locks_the_tag_and_leaves_the_others(tmp_path):
    s = Scenario(tmp_path)
    s.product(A, "Gouda slices", "100 g", 30.0)
    s.product(B, "Mozzarella slices", "100 g", 32.0)
    s.product(FILL, "Rolls", "6 ks", 30.0)
    s.history("2026-03-02", 7, 26, {A: 2, B: 1, FILL: 1})
    s.first_runs(at("2026-09-01", 9), {A: ("sliced cheese", "variety", "kg", "chilled"),
                                       B: ("sliced cheese", "variety", "kg", "chilled"),
                                       FILL: ("rolls", "cheapest", "pcs", "bakery")}, date_="2026-09-03")
    now = at("2026-09-01", 10)
    s.run(now, date_="2026-09-03")
    cart = s.cart()
    assert cart.get(A) or cart.get(B)
    edited = A if cart.get(A) else B
    other = B if edited == A else A
    s.fake.set_cart_qty(edited, cart[edited] + 1)
    env = s.run(now + timedelta(minutes=10), date_="2026-09-03")
    assert "sliced cheese" in env.data["locked"]
    assert s.cart().get(other, 0) == cart.get(other, 0), "the tag's other types are left untouched"
