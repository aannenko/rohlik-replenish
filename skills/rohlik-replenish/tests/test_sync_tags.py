"""§13.1 sync paging/coverage and tag commands."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from engine.db import connect, transaction
from engine.fake import FakeRohlik
from engine.rohlik import RohlikClient
from engine.sync import SyncError, read_delivered, store_delivered, parse_delivered
from engine.tagsvc import (TagError, prefs_never, tags_apply, tags_list, tags_merge, tags_rename, tags_set,
                           tags_storage_set)
from engine.world import load_world
from tests.helpers import Scenario, at, rohlik_time


# ------------------------------------------------------------------------------ sync
def _orders(fake: FakeRohlik, days: list[date], per_day: int = 1) -> None:
    n = 0
    for d in days:
        for _ in range(per_day):
            n += 1
            placed = at(d.isoformat(), 6) - timedelta(hours=10)
            fake.delivered.append({"id": 1000 + n, "orderTime": rohlik_time(placed),
                                   "deliverySlot": {"timeRange": f"{d.isoformat()} 08:00-09:00"},
                                   "items": [{"id": 1, "name": "x", "quantity": 1, "textualAmount": "1 l",
                                              "totalPrice": 10.0}]})


def _sync(fake, conn, e):
    client = RohlikClient(fake, sleep=lambda s: None)
    orders = [parse_delivered(o) for o in read_delivered(conn, client, e)]
    with transaction(conn):
        store_delivered(conn, orders, at(e.isoformat()))
    return client


def test_first_sync_pages_forward_from_e_minus_365_with_exclusive_date_to():
    fake = FakeRohlik()
    e = date(2026, 9, 28)
    _orders(fake, [e - timedelta(days=3 * i) for i in range(1, 50)])  # 49 orders, > 15
    conn = connect(":memory:", at("2026-09-28"))
    client = _sync(fake, conn, e)
    cached = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert cached == 49
    ranges = [a for t, a in fake.calls if t == "fetch_orders" and a.get("date_from")]
    assert ranges[0]["date_from"] == (e - timedelta(days=365)).isoformat()
    assert all(a["date_to"] == (e + timedelta(days=1)).isoformat() for a in ranges), "date_to is exclusive"
    assert all(a["limit"] == 15 for t, a in fake.calls if t == "fetch_orders")
    # slot end stored
    slot = conn.execute("SELECT slot_end FROM orders LIMIT 1").fetchone()[0]
    assert slot is not None


def test_sync_overlap_with_cache_and_page_ending_mid_day_is_deduplicated():
    fake = FakeRohlik()
    e = date(2026, 9, 28)
    days = [e - timedelta(days=100) + timedelta(days=i // 2) for i in range(40)]  # two orders per day
    _orders(fake, days)
    conn = connect(":memory:", at("2026-09-28"))
    _sync(fake, conn, e)
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 40
    # later: new orders, none of the newest cached -> pages forward from the newest cached order's dates
    more = [e + timedelta(days=i) for i in range(1, 20)]
    _orders(fake, more)
    for i, o in enumerate(fake.delivered[-19:]):
        o["id"] = 5000 + i
    _sync(fake, conn, e + timedelta(days=21))
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 59


def test_a_start_that_does_not_advance_fails_the_run():
    fake = FakeRohlik()
    e = date(2026, 9, 28)
    _orders(fake, [e - timedelta(days=100)], per_day=16)  # 16 orders on one day...
    newer = [e - timedelta(days=90 - 3 * i) for i in range(20)]
    _orders(fake, newer)  # ...and more than 15 newer ones, so the pages never meet the newest read
    for i, o in enumerate(fake.delivered[16:]):
        o["id"] = 7000 + i
    conn = connect(":memory:", at("2026-09-28"))
    with pytest.raises(SyncError):
        _sync(fake, conn, e)


def test_empty_cache_and_empty_answers():
    fake = FakeRohlik()
    conn = connect(":memory:", at("2026-09-28"))
    _sync(fake, conn, date(2026, 9, 28))
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_duplicate_lines_add_up_and_warranty_fallbacks():
    o = {"id": 1, "orderTime": "2026-07-06T15:09:36.000+0200", "deliverySlot": {"timeRange": "2026-07-07 08:00-09:00"},
         "items": [{"id": 5, "name": "Lemon", "quantity": 2, "textualAmount": "180 g", "totalPrice": 20.0},
                   {"id": 5, "name": "Lemon", "quantity": 1, "textualAmount": "180 g", "totalPrice": 0.0},
                   {"id": 6, "name": "Old line", "textualAmount": "1 l", "totalPrice": 9.0}],
         "warrantyInfo": {"enabledData": [{"productId": 5, "pieces": 3, "weighted": True, "totalWeight": 0.61,
                                           "alreadyClaimedPieces": 1}],
                          "disabledData": [{"productId": 6, "pieces": 4, "weighted": False, "totalWeight": None,
                                            "alreadyClaimedPieces": 0}]}}
    p = parse_delivered(o)
    assert p.lines[5].quantity == 3 and p.lines[5].price == 20.0 and p.lines[5].weight_kg == pytest.approx(0.61)
    assert p.lines[5].claimed == 1 and p.lines[5].weighted
    assert p.lines[6].quantity == 4, "the warranty piece count when items[].quantity is missing"
    assert p.delivery_date == date(2026, 7, 7)


# ------------------------------------------------------------------------------ tags
def _tagged(tmp_path):
    s = Scenario(tmp_path)
    s.product(1, "Milk 1.5% 1 l", "1 l", 25.0)
    s.product(2, "Milk 3.5% 1 l", "1 l", 27.0)
    s.product(3, "Bananas", "180 g", 8.0, weighted=True)
    s.product(4, "Dishwasher tablets", "80 mytí", 300.0)
    s.history("2026-06-01", 7, 12, {1: 2, 2: 1, 3: 4, 4: 1})
    s.run(at("2026-09-28", 9), date_="2026-09-30")
    return s


def test_apply_validates_units_texts_and_attributes(tmp_path):
    s = _tagged(tmp_path)
    now = at("2026-09-28", 10)
    for unit in ("l", "m"):  # §5.3: a weighed product counts by weight (kg) or in pieces (pcs), nothing else
        with pytest.raises(TagError) as err:
            tags_apply(s.conn, [{"sku": 3, "product_tag": "bananas", "policy": "cheapest", "base_unit": unit,
                                 "storage_tag": "fresh-produce"}], "agent", now)
        assert "weighed" in str(err.value)
    with pytest.raises(TagError) as err:
        tags_apply(s.conn, [{"sku": 4, "product_tag": "dishwasher tablets", "policy": "cheapest", "base_unit": "pcs",
                             "storage_tag": "drugstore-household"}], "agent", now)
    assert "units_per_pack" in str(err.value)
    res = tags_apply(s.conn, [
        {"sku": 1, "product_tag": "Milk  1.5%", "policy": "cheapest", "base_unit": "l", "storage_tag": "chilled"},
        {"sku": 2, "product_tag": "milk 3.5%", "policy": "solo", "base_unit": "l", "storage_tag": "chilled",
         "preferred": True},
        {"sku": 3, "product_tag": "bananas", "policy": "cheapest", "base_unit": "kg", "storage_tag": "fresh-produce"},
        {"sku": 4, "product_tag": "dishwasher tablets", "policy": "cheapest", "base_unit": "pcs",
         "storage_tag": "drugstore-household", "units_per_pack": 80, "stockup_mode": "on_sale"}], "agent", now)
    assert res.data["assigned"] == 4
    assert s.conn.execute("SELECT name FROM product_tags WHERE name = 'milk 1.5%'").fetchone(), "names normalised"
    with pytest.raises(TagError) as err:  # a different policy for an existing tag
        tags_apply(s.conn, [{"sku": 1, "product_tag": "milk 1.5%", "policy": "solo", "storage_tag": "chilled"}],
                   "agent", now)
    assert "tags set" in str(err.value)
    with pytest.raises(TagError):  # a stock-up mode for a short-life tag
        tags_apply(s.conn, [{"sku": 1, "product_tag": "milk 1.5%", "storage_tag": "chilled",
                             "stockup_mode": "always"}], "agent", now)
    with pytest.raises(TagError):  # a different mode for an existing tag
        tags_apply(s.conn, [{"sku": 4, "product_tag": "dishwasher tablets", "storage_tag": "drugstore-household",
                             "stockup_mode": "always"}], "agent", now)


def test_user_assignments_are_never_overwritten_by_the_agent(tmp_path):
    s = _tagged(tmp_path)
    now = at("2026-09-28", 10)
    tags_apply(s.conn, [{"sku": 1, "product_tag": "milk", "policy": "cheapest", "base_unit": "l",
                         "storage_tag": "chilled"}], "user", now)
    with pytest.raises(TagError):
        tags_apply(s.conn, [{"sku": 1, "product_tag": "milk other", "policy": "cheapest", "base_unit": "l",
                             "storage_tag": "chilled"}], "agent", now)
    assert s.conn.execute("SELECT assigned_by FROM skus WHERE sku = 1").fetchone()[0] == "user"


def test_rename_merge_set_keep_tag_id_prefs_and_history(tmp_path):
    s = _tagged(tmp_path)
    now = at("2026-09-28", 10)
    tags_apply(s.conn, [
        {"sku": 1, "product_tag": "milk low", "policy": "cheapest", "base_unit": "l", "storage_tag": "dry"},
        {"sku": 2, "product_tag": "milk full", "policy": "cheapest", "base_unit": "l", "storage_tag": "dry",
         "stockup_mode": "on_sale"},
        {"sku": 3, "product_tag": "bananas", "policy": "cheapest", "base_unit": "kg", "storage_tag": "fresh-produce"},
        {"sku": 4, "ignore": True}], "agent", now)
    low = s.conn.execute("SELECT tag_id FROM product_tags WHERE name = 'milk low'").fetchone()[0]
    prefs_never(s.conn, "add", "milk low", now)
    tags_rename(s.conn, "milk low", "milk 1.5%", "user", now)
    row = s.conn.execute("SELECT tag_id, source FROM product_tags WHERE name = 'milk 1.5%'").fetchone()
    assert row[0] == low and row[1] == "user"
    assert s.conn.execute("SELECT COUNT(*) FROM prefs WHERE tag_id = ?", (low,)).fetchone()[0] == 1
    # merge: INTO keeps its attributes, takes FROM's mode when it has none; prefs move, FROM disappears
    tags_merge(s.conn, "milk full", "milk 1.5%", "user", now)
    row = s.conn.execute("SELECT stockup_mode FROM product_tags WHERE tag_id = ?", (low,)).fetchone()
    assert row[0] == "on_sale"
    assert s.conn.execute("SELECT COUNT(*) FROM product_tags WHERE name = 'milk full'").fetchone()[0] == 0
    world = load_world(s.conn)
    assert sorted(world.tags[low].skus) == [1, 2]
    # set: policy and a base-unit change validated against weighed SKUs (kg or pcs only)
    tags_set(s.conn, "milk 1.5%", preferred=2, note="the kids like it", source="user", now=now)
    assert s.conn.execute("SELECT policy, preferred_sku FROM product_tags WHERE tag_id = ?",
                          (low,)).fetchone()[:] == ("solo", 2)
    with pytest.raises(TagError):
        tags_set(s.conn, "bananas", base_unit="l", source="user", now=now)
    res = tags_set(s.conn, "milk 1.5%", base_unit="kg", source="user", now=now)
    assert res.warnings, "SKUs whose size becomes unknown are reported"
    # storage flags
    tags_storage_set(s.conn, "dry", False, now)
    assert s.conn.execute("SELECT stockup_eligible FROM storage_tags WHERE name = 'dry'").fetchone()[0] == 0
    md = tags_list(s.conn)
    assert "Storage tags" in md and "milk 1.5%" in md and "Ignored products" in md


def test_moving_the_last_sku_deletes_the_tag_and_reports_dropped_prefs(tmp_path):
    s = _tagged(tmp_path)
    now = at("2026-09-28", 10)
    tags_apply(s.conn, [{"sku": 1, "product_tag": "milk a", "policy": "cheapest", "base_unit": "l",
                         "storage_tag": "chilled"}], "agent", now)
    prefs_never(s.conn, "add", "milk a", now)
    res = tags_apply(s.conn, [{"sku": 1, "product_tag": "milk b", "policy": "cheapest", "base_unit": "l",
                               "storage_tag": "chilled"}], "agent", now)
    assert any("milk a" in m and "dropped" in m for m in res.messages)


# ------------------------------------------------------------------------------ weighed products
BAN, BUNCH, POTATO, MILK1 = 3, 5, 6, 1


def _weighed(tmp_path):
    """Loose bananas, a weighed bunch of 4, loose potatoes; 16 weekly orders of 6 bananas whose weight alternates
    1.5 kg / 0.9 kg, one piece claimed in the fourth order."""
    s = Scenario(tmp_path)
    s.product(MILK1, "Milk 1 l", "1 l", 25.0)
    s.product(BAN, "Banán 1 ks", "cca 190 g", 8.0, weighted=True)
    s.product(BUNCH, "Banány, balení", "cca 0,65 kg", 30.0, weighted=True)
    s.product(POTATO, "Brambora 1 ks", "cca 190 g", 3.0, weighted=True)
    day = date(2026, 6, 1)
    for i in range(16):
        s.delivered(day.isoformat(), {MILK1: 2, BAN: 6, POTATO: 5}, weights={BAN: 0.9 if i % 2 else 1.5, POTATO: 0.95},
                    claims={BAN: 1} if i == 3 else None)
        day += timedelta(days=7)
    s.delivered("2026-07-15", {BUNCH: 1}, weights={BUNCH: 0.71})
    s.run(at("2026-09-28", 9), date_="2026-09-30")  # needs_tags
    return s


def test_apply_accepts_a_weighed_sku_in_a_pcs_tag_without_units_per_pack(tmp_path):
    s = _weighed(tmp_path)
    now = at("2026-09-28", 10)
    res = tags_apply(s.conn, [
        {"sku": BAN, "product_tag": "bananas", "policy": "cheapest", "base_unit": "pcs", "storage_tag": "fresh-produce"},
        {"sku": BUNCH, "product_tag": "bananas", "storage_tag": "fresh-produce", "units_per_pack": 4},
        {"sku": POTATO, "product_tag": "potatoes", "policy": "cheapest", "base_unit": "kg",
         "storage_tag": "fresh-produce"},
        {"sku": MILK1, "product_tag": "milk", "policy": "cheapest", "base_unit": "l", "storage_tag": "chilled"}],
        "agent", now)
    assert res.data["assigned"] == 4 and not res.warnings
    row = s.conn.execute("SELECT upp_override, upp_unit, upp_source FROM skus WHERE sku = ?", (BUNCH,)).fetchone()
    assert tuple(row) == (4.0, "pcs", "agent"), "a weighed bunch gives its piece count"
    assert s.conn.execute("SELECT upp_override FROM skus WHERE sku = ?", (BAN,)).fetchone()[0] is None
    md = tags_list(s.conn, "bananas")
    assert "weighed products counted in pieces" in md
    assert "'cca 190 g' → 1 pcs" in md and "'cca 0,65 kg' → 4 pcs (set by agent)" in md
    assert "weighed products counted by weight" in tags_list(s.conn, "potatoes")
    # a non-weighed SKU whose text is a weight still needs units_per_pack in a pcs tag
    s.product(9, "Garlic net", "250 g", 20.0)
    s.fake.set_cart_qty(9, 1)
    s.run(at("2026-09-28", 11), date_="2026-09-30")
    with pytest.raises(TagError) as err:
        tags_apply(s.conn, [{"sku": 9, "product_tag": "garlic", "policy": "cheapest", "base_unit": "pcs",
                             "storage_tag": "fresh-produce"}], "agent", now)
    assert "units_per_pack" in str(err.value)


def test_merge_and_set_keep_weighed_products_in_kg_or_pcs_tags(tmp_path):
    s = _weighed(tmp_path)
    now = at("2026-09-28", 10)
    tags_apply(s.conn, [
        {"sku": BAN, "product_tag": "bananas", "policy": "cheapest", "base_unit": "kg", "storage_tag": "fresh-produce"},
        {"sku": BUNCH, "product_tag": "banana bunch", "policy": "cheapest", "base_unit": "kg",
         "storage_tag": "fresh-produce"},
        {"sku": POTATO, "product_tag": "potatoes", "policy": "cheapest", "base_unit": "kg",
         "storage_tag": "fresh-produce"},
        {"sku": MILK1, "product_tag": "milk", "policy": "cheapest", "base_unit": "l", "storage_tag": "chilled"}],
        "agent", now)
    with pytest.raises(TagError) as err:
        tags_merge(s.conn, "potatoes", "milk", "user", now)
    assert "weighed" in str(err.value)
    for unit in ("l", "m"):
        with pytest.raises(TagError):
            tags_set(s.conn, "bananas", base_unit=unit, source="user", now=now)
    res = tags_set(s.conn, "bananas", base_unit="pcs", source="user", now=now)
    assert res.warnings and "Banán 1 ks" in res.warnings[-1] and "units_per_pack" in res.warnings[-1], \
        "the agent is reminded that a bunch needs its piece count"
    res = tags_merge(s.conn, "banana bunch", "bananas", "user", now)
    assert any("Banány, balení" in w for w in res.warnings)
    res = tags_apply(s.conn, [{"sku": BUNCH, "product_tag": "bananas", "storage_tag": "fresh-produce",
                               "units_per_pack": 4}], "user", now)
    assert s.conn.execute("SELECT upp_override FROM skus WHERE sku = ?", (BUNCH,)).fetchone()[0] == 4.0
    # back to kg: the bunch's piece count is never reinterpreted as a weight, so its size is unknown until fixed
    res = tags_set(s.conn, "bananas", base_unit="kg", source="user", now=now)
    assert any("Banány, balení" in w and "size unknown in kg" in w for w in res.warnings)


def test_switching_a_weighed_tag_between_kg_and_pcs_recounts_its_history(tmp_path):
    from engine.planner import build_events
    from tests.test_scenarios_extra import _state

    s = _weighed(tmp_path)
    now = at("2026-09-28", 10)
    s.tag_all({MILK1: ("milk", "cheapest", "l", "chilled"), BAN: ("bananas", "cheapest", "kg", "fresh-produce"),
               BUNCH: ("bananas", "cheapest", "kg", "fresh-produce"),
               POTATO: ("potatoes", "cheapest", "kg", "fresh-produce")}, now)

    def history_q():
        world, state = _state(s, now, now.date())
        tag_id = next(t.tag_id for t in world.tags.values() if t.name == "bananas")
        return [round(ev.q, 6) for ev in build_events(world, state, now.date(), {})[tag_id] if ev.history]

    by_weight = history_q()
    assert by_weight[:5] == [1.5, 0.9, 1.5, 0.75, 1.5], "kg: actual weights, a claim scales its line"
    assert 0.71 in by_weight, "the bunch by its weight"
    tags_set(s.conn, "bananas", base_unit="pcs", source="user", now=now)
    tags_apply(s.conn, [{"sku": BUNCH, "product_tag": "bananas", "storage_tag": "fresh-produce",
                         "units_per_pack": 4}], "user", now)
    in_pieces = history_q()
    assert in_pieces[:5] == [6.0, 6.0, 6.0, 5.0, 6.0], "pcs: pieces minus claimed pieces, whatever they weighed"
    assert 4.0 in in_pieces, "the bunch counts its 4 pieces"
    tags_set(s.conn, "bananas", base_unit="kg", source="user", now=now)
    assert history_q()[:5] == by_weight[:5], "and back"
    env = s.run(at("2026-09-28", 11), date_="2026-09-30", dry=True)
    rows = [r for r in env.data["plan"] if r["tag"] == "bananas"]
    assert rows and {r["size_unit"] for r in rows} == {"kg"}, s.dump(env)
    tags_set(s.conn, "bananas", base_unit="pcs", source="user", now=now)
    env = s.run(at("2026-09-28", 12), date_="2026-09-30", dry=True)
    rows = [r for r in env.data["plan"] if r["tag"] == "bananas"]
    assert rows and {(r["size_unit"], r["size"]) for r in rows} <= {("pcs", 1.0), ("pcs", 4.0)}, s.dump(env)


def test_setup_report_and_tag_check_say_how_weighed_products_are_counted(tmp_path):
    from engine.summary import render  # noqa: F401 (the setup report is part of the first run's summary)

    s = _weighed(tmp_path)
    mapping = {MILK1: ("milk", "cheapest", "l", "chilled"),
               BAN: ("bananas", "cheapest", "pcs", "fresh-produce"),
               BUNCH: ("bananas", "cheapest", "pcs", "fresh-produce", {"units_per_pack": 4}),
               POTATO: ("potatoes", "cheapest", "kg", "fresh-produce")}
    s.tag_all(mapping, at("2026-09-28", 9))
    env = s.run(at("2026-09-28", 9, 5), date_="2026-09-30")
    assert env.status == "dry_run", env.summary_md
    full = Path(env.data["report_path"]).read_text(encoding="utf-8")  # §8.2: the full report is in its file
    # loose bananas are 16 of 17 purchases and 7 % pricier per piece than the bunch: the first run guesses solo
    banana_line = next(x for x in full.splitlines() if x.startswith("- **bananas** · "))
    assert banana_line.startswith("- **bananas** · solo: prefers Banán 1 ks — 16 of 17 purchases, 7 % pricier than "
                                  "the others you bought · pcs · counted in pieces"), banana_line
    assert "- **potatoes** · cheapest · kg · by weight" in full
    assert "- **milk** · cheapest · l\n" in full, "no label without weighed products"
    assert "  - Banány, balení · ⚖ 4 pcs · bought 1×" in full and "  - Banán 1 ks · ⚖ 1 pcs · bought 16×" in full
    assert "count a tag's weighed products (⚖) in pieces or by weight" in full
    md = env.summary_md  # the compact version: one line per tag, products inline
    assert "· pcs · counted in pieces — **2 products, solo, guessed solo**: Banán 1 ks · ⚖ 1 pcs · 16×; " \
           "Banány, balení · ⚖ 4 pcs · 1×" in md
    assert "- **potatoes** · cheapest · kg · by weight: Brambora 1 ks" in md
    # a weighed product tagged later is listed with its counting
    s.product(7, "Jablko 1 ks", "cca 180 g", 6.0, weighted=True)
    s.delivered("2026-09-29", {7: 3}, weights={7: 0.55})
    env = s.run(at("2026-09-30", 9), date_="2026-10-02")
    assert env.status == "needs_tags", env.summary_md
    s.tag_all({7: ("apples", "cheapest", "pcs", "fresh-produce")}, at("2026-09-30", 9, 5))
    env = s.run(at("2026-09-30", 9, 10), date_="2026-10-02")
    assert env.status == "ok", env.summary_md
    assert "- Fresh produce — apples (cheapest, counted in pieces): Jablko 1 ks" in env.summary_md
