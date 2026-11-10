"""§13.1 stock-up modes and amounts, once per sale, assessment, budget; §13.2 sale scenarios."""

from datetime import timedelta

from tests.helpers import Scenario, at

PASTA, PASTA2, RICE, OIL, MILK = 201, 202, 203, 204, 101


def base(tmp_path, *, mode="on_sale", budget=None, paid=None, pasta2=False):
    """`pasta2` seeds one purchase of PASTA2 in the pasta tag, so it is a real candidate."""
    from engine.paths import Config

    s = Scenario(tmp_path, config=Config(stockup_budget_czk=budget) if budget is not None else None)
    s.product(PASTA, "Pasta Fusilli 500 g", "500 g", 40.0)
    s.product(PASTA2, "Pasta Penne 500 g", "500 g", 44.0)
    s.product(RICE, "Rice 1 kg", "1 kg", 50.0)
    s.product(MILK, "Milk 1 l", "1 l", 25.0)
    qty = [2, 4, 2, 2, 2, 4, 2, 2, 2, 2, 2, 2, 2]
    day = at("2026-04-01").date()
    for i, q in enumerate(qty):
        lines = {PASTA: q, MILK: 2, RICE: 1}
        if pasta2 and i == 0:
            lines[PASTA2] = 1
        s.delivered(day.isoformat(), lines, prices=paid)
        day += timedelta(days=14)
    s.history("2026-04-08", 14, 13, {MILK: 2})
    extra = {"stockup_mode": mode} if mode else {}
    mapping = {PASTA: ("pasta", "cheapest", "kg", "dry", extra), PASTA2: ("pasta", "cheapest", "kg", "dry"),
               RICE: ("rice", "cheapest", "kg", "dry", {"stockup_mode": "always"}),
               MILK: ("milk", "cheapest", "l", "chilled")}
    s.first_runs(at("2026-09-28", 9), {k: v for k, v in mapping.items() if pasta2 or k != PASTA2},
                 date_="2026-09-30")
    return s


def rows_of(env, tag):
    return [r for r in env.data["plan"] if r["tag"] == tag]


def test_on_sale_tops_up_to_max_from_the_qualifying_pool(tmp_path):
    s = base(tmp_path)
    s.set_price(PASTA, sale=24.0, sale_until="2026-10-03")  # -40 %
    env = s.run(at("2026-09-28", 10), date_="2026-09-30")
    rows = rows_of(env, "pasta")
    assert rows and all(r["sku"] == PASTA for r in rows), s.dump(env)
    r = rows[0]
    assert r["stockup_units"] > 0 and r["sale_until"] == "2026-10-03"
    assert r["reason"] in ("due", "stockup")
    # never takes this order above MAX (4 packs = 2 kg) by the top-up
    assert (r["normal_units"] + r["stockup_units"]) * 0.5 <= 2.0 + 1e-9
    # §13.2: a writing run lists no added lines, only one line for the top-up (§8.1)
    md = env.summary_md
    assert env.status == "ok" and s.cart().get(MILK), "milk was written too"
    assert "Would add" not in md and "Added" not in md and "Milk 1 l" not in md
    section = md.split("### Topped up on sale")[1].split("\n### ")[0].strip().splitlines()
    assert len(section) == 1 and section[0].startswith("- **Pasta Fusilli 500 g** · +") and "until 3 Oct" in section[0]
    assert "−40 %" in section[0] or "-40 %" in section[0], section[0]


def test_on_sale_without_a_qualifying_sale_is_wait_sale_when_due(tmp_path):
    s = base(tmp_path)
    s.set_price(PASTA, sale=30.0, sale_until="2026-10-03")  # -25 %: counts, doesn't qualify
    env = s.run(at("2026-10-10", 10), date_="2026-10-12")
    rows = rows_of(env, "pasta")
    assert rows and rows[0]["reason"] == "wait_sale", s.dump(env)
    assert rows[0]["normal_units"] == 0
    assert "running low — waiting for a sale" in env.summary_md
    assert PASTA not in s.cart()


def test_exactly_35_percent_qualifies_and_34_9_does_not(tmp_path):
    s = base(tmp_path)
    s.set_price(PASTA, sale=26.0, sale_until="2026-10-03")  # exactly 35 %
    env = s.run(at("2026-09-28", 10), date_="2026-09-30", dry=True)
    assert any(r["stockup_units"] > 0 for r in rows_of(env, "pasta"))
    s.set_price(PASTA, sale=26.04, sale_until="2026-10-03")  # 34.9 %
    env = s.run(at("2026-09-28", 11), date_="2026-09-30", dry=True)
    assert not any(r["stockup_units"] > 0 for r in rows_of(env, "pasta"))


def test_sale_ending_today_no_longer_counts(tmp_path):
    s = base(tmp_path)
    s.set_price(PASTA, sale=20.0, sale_until="2026-09-28T23:59:59+02:00")
    env = s.run(at("2026-09-28", 10), date_="2026-09-30", dry=True)
    assert not any(r["stockup_units"] > 0 for r in rows_of(env, "pasta"))


def test_null_original_price_never_qualifies(tmp_path):
    s = base(tmp_path)
    s.set_price(PASTA, sale=10.0, sale_until="2026-10-03", original=None)
    env = s.run(at("2026-09-28", 10), date_="2026-09-30", dry=True)
    assert not any(r["stockup_units"] > 0 for r in rows_of(env, "pasta"))


def test_once_per_sale(tmp_path):
    s = base(tmp_path, pasta2=True)
    s.set_price(PASTA, sale=24.0, sale_until="2026-10-08")
    now = at("2026-09-28", 10)
    env = s.run(now, date_="2026-09-30")
    assert s.cart().get(PASTA)
    topped = s.cart()[PASTA]
    # a re-run in the same cycle plans the same top-up again and writes nothing
    n = len(s.fake.writes)
    env = s.run(now + timedelta(minutes=10), date_="2026-09-30")
    assert len(s.fake.writes) == n and s.cart()[PASTA] == topped
    # the order closes the cycle; during the same sale: no new top-up
    oid = s.checkout(now + timedelta(minutes=30), "2026-09-30")
    s.run(now + timedelta(hours=2), date_="2026-09-30")
    s.deliver(oid)
    env = s.run(at("2026-10-02", 10), date_="2026-10-04")
    assert not any(r["stockup_units"] > 0 for r in rows_of(env, "pasta")), s.dump(env)
    # another deep sale of the tag during the wait, on another SKU of the tag that is now the cheapest, so it heads
    # the qualifying pool: still no top-up (PASTA2 is a bought, tagged candidate)
    s.set_price(PASTA2, sale=20.0, sale_until="2026-10-20")
    env = s.run(at("2026-10-02", 11), date_="2026-10-04")
    assert any(a.get("product_ids") and PASTA2 in a["product_ids"] for t, a in s.fake.calls[-12:]
               if t == "get_product_details")
    assert not any(r["stockup_units"] > 0 for r in rows_of(env, "pasta"))
    # from the day the first sale ends, a new qualifying sale tops up again, on the cheapest qualifying SKU
    s.set_price(PASTA, sale=24.0, sale_until="2026-10-20")
    env = s.run(at("2026-10-08", 10), date_="2026-10-10")
    tops = [r for r in rows_of(env, "pasta") if r["stockup_units"] > 0]
    assert tops and tops[0]["sku"] == PASTA2, s.dump(env)


def test_removed_top_up_still_starts_the_wait(tmp_path):
    s = base(tmp_path)
    s.set_price(PASTA, sale=24.0, sale_until="2026-10-08")
    now = at("2026-09-28", 10)
    s.run(now, date_="2026-09-30")
    s.fake.set_cart_qty(PASTA, 0)  # the user deletes the top-up
    oid = s.checkout(now + timedelta(minutes=30), "2026-09-30")
    s.run(now + timedelta(hours=2), date_="2026-09-30")
    s.deliver(oid)
    env = s.run(at("2026-10-02", 10), date_="2026-10-04")
    assert not any(r["stockup_units"] > 0 for r in rows_of(env, "pasta"))


def test_undated_top_up_waits_seven_days(tmp_path):
    s = base(tmp_path)
    s.set_price(PASTA, sale=24.0, sale_until=None)
    now = at("2026-09-28", 10)
    s.run(now, date_="2026-09-30")
    assert s.cart().get(PASTA)
    oid = s.checkout(now + timedelta(minutes=30), "2026-09-30")
    s.run(now + timedelta(hours=2), date_="2026-09-30")
    s.deliver(oid)
    env = s.run(at("2026-10-03", 10), date_="2026-10-05")
    assert not any(r["stockup_units"] > 0 for r in rows_of(env, "pasta"))
    env = s.run(at("2026-10-06", 10), date_="2026-10-08")
    assert any(r["stockup_units"] > 0 for r in rows_of(env, "pasta")), s.dump(env)


def test_always_tag_ignores_sales(tmp_path):
    s = base(tmp_path)
    plain = s.run(at("2026-10-10", 10), date_="2026-10-12", dry=True)
    s.set_price(RICE, sale=20.0, sale_until="2026-10-20")
    sale = s.run(at("2026-10-10", 11), date_="2026-10-12", dry=True)
    a, b = rows_of(plain, "rice"), rows_of(sale, "rice")
    assert [(r["normal_units"], r["stockup_units"]) for r in a] == [(r["normal_units"], r["stockup_units"]) for r in b]
    assert all(r["stockup_units"] == 0 for r in b)


def test_budget_trims_only_stockup_parts(tmp_path):
    s = base(tmp_path, budget=30.0)
    s.set_price(PASTA, sale=24.0, sale_until="2026-10-08")
    env = s.run(at("2026-09-28", 10), date_="2026-09-30", dry=True)
    rows = rows_of(env, "pasta")
    spend = sum(r["stockup_units"] * 24.0 for r in rows)
    assert spend <= 30.0 + 1e-9
    s2 = base(tmp_path / "b", budget=0.0)
    s2.set_price(PASTA, sale=24.0, sale_until="2026-10-08")
    env = s2.run(at("2026-09-28", 10), date_="2026-09-30", dry=True)
    rows = rows_of(env, "pasta")
    assert all(r["stockup_units"] == 0 for r in rows)
    if rows and rows[0]["reason"] != "due":
        assert rows[0]["reason"] == "budget_limit"
        assert "stock-up budget reached" in env.summary_md


def test_assessment_from_paid_prices(tmp_path):
    # pasta bought at 24 CZK while it costs 40 today: more than half on sale -> on_sale
    s = base(tmp_path, mode=None, paid={PASTA: 24.0})
    env = s.envelopes[-1]
    assert env.data["assessed"].get("pasta") == "on_sale", s.dump(env)
    assert "stock-up: on_sale — 30 of 30 packs bought at least 35 % below today's regular price" in env.summary_md
    mode = s.conn.execute("SELECT stockup_mode FROM product_tags WHERE name = 'pasta'").fetchone()[0]
    assert mode == "on_sale", "the first run (a dry run) stores the guessed mode"
    # never reassessed
    env = s.run(at("2026-09-28", 10), date_="2026-09-30", dry=True)
    assert "pasta" not in env.data["assessed"]


def test_assessment_without_evidence_is_always_and_later_tags_default_to_always(tmp_path):
    """Only the first run guesses from prices; no usable line means always; after setup a long-life tag
    reaching n >= 3 without a mode gets always, even when every pack was bought deep on sale."""
    s = base(tmp_path, mode=None)
    assert s.envelopes[-1].data["assessed"].get("pasta") == "always"
    s2 = Scenario(tmp_path / "b")
    s2.product(PASTA, "Pasta 500 g", "500 g", 40.0)
    s2.product(MILK, "Milk 1 l", "1 l", 25.0)
    s2.history("2026-04-01", 14, 13, {PASTA: 2, MILK: 2})
    del s2.fake.products[PASTA]  # no live price: no usable line
    env = s2.first_runs(at("2026-09-28", 9), {PASTA: ("pasta", "cheapest", "kg", "dry"),
                                               MILK: ("milk", "cheapest", "l", "chilled")}, date_="2026-09-30")
    assert env.data["assessed"].get("pasta") == "always"
    assert "stock-up: always (no price evidence)" in env.summary_md
    stored = s2.conn.execute("SELECT stockup_mode FROM product_tags WHERE name = 'pasta'").fetchone()[0]
    assert stored == "always"
    s3 = Scenario(tmp_path / "c")
    s3.product(RICE, "Rice 1 kg", "1 kg", 50.0)
    s3.product(MILK, "Milk 1 l", "1 l", 25.0)
    s3.history("2026-04-01", 7, 26, {MILK: 2})
    s3.delivered("2026-08-01", {RICE: 2}, prices={RICE: 20.0})
    s3.delivered("2026-09-01", {RICE: 2}, prices={RICE: 20.0})
    env = s3.first_runs(at("2026-09-28", 9), {RICE: ("rice", "cheapest", "kg", "dry"),
                                               MILK: ("milk", "cheapest", "l", "chilled")}, date_="2026-09-30")
    assert "rice" not in env.data["assessed"], "n = 2: no mode yet"
    s3.delivered("2026-10-01", {RICE: 2}, prices={RICE: 20.0})  # a third purchase, again 60 % below
    env = s3.run(at("2026-10-05", 9), date_="2026-10-07")
    assert env.data["assessed"].get("rice") == "always", s3.dump(env)
    stored = s3.conn.execute("SELECT stockup_mode FROM product_tags WHERE name = 'rice'").fetchone()[0]
    assert stored == "always", "only the first run guesses from prices"


def test_assessment_uses_a_weighed_lines_weight_only_in_a_kg_tag(tmp_path):
    """§6.14: gross base quantity = weight_kg for a weighed line in a kg tag, else quantity × size. Frozen fish
    fillets at 60 CZK a piece, today 80 a piece (cca 250 g). The pieces delivered were heavy (2 weighed 0.7 kg):
    by weight that's 171 CZK/kg against today's 320, a deep sale; in pieces, 60 against 80, not 35 % off."""
    fish = 210
    for unit, expected in (("kg", "on_sale"), ("pcs", "always")):
        s = Scenario(tmp_path / unit)
        s.product(MILK, "Milk 1 l", "1 l", 25.0)
        s.product(fish, "Fish fillet 1 ks", "cca 250 g", 80.0, weighted=True)
        s.history("2026-06-01", 7, 16, {MILK: 2})
        for day in ("2026-07-01", "2026-08-01", "2026-09-01"):
            s.delivered(day, {fish: 2}, weights={fish: 0.7}, prices={fish: 60.0})
        env = s.first_runs(at("2026-09-28", 9), {MILK: ("milk", "cheapest", "l", "chilled"),
                                                  fish: ("fish fillet", "cheapest", unit, "frozen")},
                           date_="2026-09-30")
        assert env.data["assessed"].get("fish fillet") == expected, (unit, s.dump(env))


def test_short_life_tag_rejects_a_stockup_mode(tmp_path):
    from engine.tagsvc import TagError, tags_set

    s = base(tmp_path)
    try:
        tags_set(s.conn, "milk", stockup_mode="on_sale", source="user", now=at("2026-09-28", 10))
        raise AssertionError("expected a TagError")
    except TagError as exc:
        assert "short-life" in str(exc)
