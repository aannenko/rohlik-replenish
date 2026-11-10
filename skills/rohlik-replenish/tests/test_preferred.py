"""§13.1 Preferred products and the 1 -> 2 migration: the migration on real version-1 databases, the first
run's guess, the solo fallback and its reminder, and the tag commands' preferred-product rules (§5.3, §6.13)."""

import json
import shutil
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from engine import cli
from engine.db import DbError, connect, migrate
from engine.status import status
from engine.tagsvc import TagError, tags_apply, tags_list, tags_merge, tags_set
from engine.timeutil import iso, parse_iso
from tests.helpers import RECORDED as FIX
from tests.helpers import Scenario, at, recorded

V1 = Path(__file__).parent / "fixtures" / "v1"
TABLES = ("meta", "storage_tags", "product_tags", "skus", "orders", "order_lines", "cancelled_orders", "prefs", "runs",
          "run_upcoming_orders", "run_plan", "run_cart", "run_edits", "run_calls")


# ------------------------------------------------------------------------------ the 1 -> 2 migration (§5.1)
def v1_db(directory: Path, name: str) -> Path:
    """A real version-1 database: a dump made by the last build with schema version 1 (tests/fixtures/v1/README.md)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "replenish.db"
    raw = sqlite3.connect(path)
    raw.executescript((V1 / f"{name}.sql").read_text(encoding="utf-8"))
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()
    return path


def counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def independent_fill(conn, e: date) -> dict[str, int]:
    """§5.3's fill, computed independently of the engine: per solo tag, the SKU in the most window orders with a
    positive unclaimed quantity (ties: the latest such order, then the lower id); else the most recently bought."""
    elig = dict(conn.execute("SELECT name, stockup_eligible FROM storage_tags").fetchall())
    out = {}
    for tag_id, name in conn.execute("SELECT tag_id, name FROM product_tags WHERE policy = 'solo'").fetchall():
        skus = conn.execute("SELECT sku, storage_tag FROM skus WHERE product_tag_id = ?", (tag_id,)).fetchall()
        start = (e - timedelta(days=365 if all(elig[st] for _, st in skus) else 183)).isoformat()
        rows = conn.execute(
            f"SELECT l.sku, o.delivery_date, o.order_time FROM order_lines l JOIN orders o USING (order_id) "
            f"WHERE l.sku IN ({','.join(str(s) for s, _ in skus)}) AND l.quantity - l.claimed_quantity > 0").fetchall()
        cnt, last = {}, {}
        for sku, day, placed in rows:
            if start < day < e.isoformat():
                cnt[sku] = cnt.get(sku, 0) + 1
                last[sku] = max(last.get(sku, ("", "")), (day, placed))
        if cnt:
            out[name] = max(cnt, key=lambda s: (cnt[s], last[s], -s))
        else:
            out[name] = max(((d, t, s) for s, d, t in rows), key=lambda x: (x[0], x[1], -x[2]))[2] if rows else \
                min(s for s, _ in skus)
    return out


MIGRATED = parse_iso("2026-10-28T12:00:00+01:00")


@recorded
def test_the_migration_on_the_recorded_version_1_database(tmp_path):
    """§13.1: a real version-1 database with solo tags, a never list, user-source rows and stock-up modes:
    everything survives, each solo tag gets its largest-share SKU, the CHECKs hold, and reopening is a no-op."""
    path = v1_db(tmp_path, "recorded")
    raw = sqlite3.connect(path)
    before = counts(raw)
    tag_cols = "tag_id, name, policy, base_unit, stockup_mode, note, source, updated_at"
    old_tags = raw.execute(f"SELECT {tag_cols} FROM product_tags ORDER BY tag_id").fetchall()
    old_skus = raw.execute("SELECT * FROM skus ORDER BY sku").fetchall()
    expected = independent_fill(raw, MIGRATED.date())
    raw.close()
    assert len(expected) == 19 and before["prefs"] == 1

    conn = connect(str(path), MIGRATED)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    schema = " ".join(r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    assert "product_tags_v2" not in schema and "REFERENCES product_tags(tag_id)" in schema
    after = counts(conn)
    assert after == {**before, "meta": before["meta"] + 1}, "every row survives; meta gains preferred_check"
    assert conn.execute("SELECT value FROM meta WHERE key = 'preferred_check'").fetchone()[0] == iso(MIGRATED)
    assert [tuple(r) for r in conn.execute(f"SELECT {tag_cols} FROM product_tags ORDER BY tag_id")] == old_tags
    assert [tuple(r) for r in conn.execute("SELECT * FROM skus ORDER BY sku")] == old_skus
    fill = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT name, preferred_sku, preferred_source FROM product_tags WHERE policy = 'solo'")}
    assert {k: v[0] for k, v in fill.items()} == expected
    assert {v[1] for v in fill.values()} == {"agent"}, "the migration's choices count as the agent's"
    assert fill["eggs"][0] == 1316375, "Pohodová vejce L: 9 of the 15 purchases in the window"
    assert conn.execute("SELECT COUNT(*) FROM product_tags WHERE policy <> 'solo' AND (preferred_sku IS NOT NULL "
                        "OR preferred_source IS NOT NULL)").fetchone()[0] == 0
    for name, (sku, _) in fill.items():  # one of the tag's own SKUs
        assert conn.execute("SELECT t.name FROM skus s JOIN product_tags t ON t.tag_id = s.product_tag_id "
                            "WHERE s.sku = ?", (sku,)).fetchone()[0] == name
    # the user's state survives: the never list, a user-set mode, a note, user-source rows
    assert [tuple(r) for r in conn.execute("SELECT t.name FROM prefs p JOIN product_tags t USING (tag_id)")] == [("coconut milk",)]
    assert conn.execute("SELECT stockup_mode, source FROM product_tags WHERE name = 'oat drink'").fetchone()[:] == \
        ("on_sale", "user")
    assert conn.execute("SELECT note FROM product_tags WHERE name = 'eggs'").fetchone()[0] == "free-range only"
    assert conn.execute("SELECT COUNT(*) FROM skus WHERE assigned_by = 'user'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM run_plan WHERE fallback_from IS NOT NULL").fetchone()[0] == 0
    # the CHECKs hold
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE product_tags SET preferred_sku = NULL, preferred_source = NULL WHERE name = 'eggs'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE product_tags SET preferred_source = NULL WHERE name = 'eggs'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE product_tags SET preferred_sku = 1316375, preferred_source = 'agent' WHERE name = 'butter'")
    with pytest.raises(sqlite3.IntegrityError):  # the circular reference is enforced: no unknown SKU
        conn.execute("UPDATE product_tags SET preferred_sku = 99999999 WHERE name = 'eggs'")
    dump = list(conn.iterdump())
    conn.close()
    again = connect(str(path), MIGRATED + timedelta(days=1))  # reopening is a no-op
    assert list(again.iterdump()) == dump
    again.close()


def test_the_migration_fill_rules_on_the_synthetic_version_1_database(tmp_path):
    """§5.3 fill: the largest share (not the latest purchase), a share tie to the latest purchase, and without a
    window purchase the most recently bought SKU, then the lower id; a SKU never bought is its tag's own."""
    path = v1_db(tmp_path, "scenario")
    now = parse_iso("2026-10-01T10:00:00+02:00")
    raw = sqlite3.connect(path)
    expected = independent_fill(raw, now.date())
    raw.close()
    conn = connect(str(path), now)
    fill = dict(conn.execute("SELECT name, preferred_sku FROM product_tags WHERE policy = 'solo'").fetchall())
    assert fill == expected == {
        "eggs size l": 202,  # 5 of 9 window purchases, although 201 was bought more recently
        "coffee beans": 302,  # 2 : 2 in the 12-month window; 302 was bought last
        "baby food jar": 401,  # no purchase in the 6-month window: the most recently bought
        "rice cakes": 501,  # last bought together in one order: the lower id
        "oat milk barista": 601}  # only ever in the cart
    assert conn.execute("SELECT stockup_mode, source FROM product_tags WHERE name = 'coffee beans'").fetchone()[:] == \
        ("on_sale", "user"), "a user-set mode and source survive; the preferred product is the agent's to check"
    conn.close()


def test_the_fill_without_a_window_purchase_takes_the_most_recent_then_the_lower_id(tmp_path):
    """§5.3: the fill reads only columns both schemas share, so it runs here on an engine-built household where the
    most recently bought SKU has the higher id."""
    from engine.preferred import migration_fill

    s = Scenario(tmp_path)
    s.product(BREAD, "Bread 500 g", "500 g", 40.0)
    for sku in (401, 402, 501, 502):
        s.product(sku, f"Jar {sku} 190 g", "190 g", 30.0)
    for day in ("2025-11-13", "2025-12-11", "2026-01-08"):
        s.delivered(day, {401: 1})
    s.delivered("2026-03-19", {402: 1})  # the latest, but outside the 6-month window on 1 Oct
    s.delivered("2026-02-19", {501: 1, 502: 1})
    s.history("2026-08-06", 7, 8, {BREAD: 1})
    assert s.run(at("2026-10-01", 9), date_="2026-10-03").status == "needs_tags"
    tags_apply(s.conn, [{"sku": BREAD, "product_tag": "bread", "policy": "cheapest", "base_unit": "kg",
                         "storage_tag": "bakery"},
                        {"sku": 401, "product_tag": "jar a", "policy": "solo", "base_unit": "kg",
                         "storage_tag": "chilled", "preferred": True},
                        {"sku": 402, "product_tag": "jar a", "storage_tag": "chilled"},
                        {"sku": 501, "product_tag": "jar b", "policy": "solo", "base_unit": "kg",
                         "storage_tag": "chilled", "preferred": True},
                        {"sku": 502, "product_tag": "jar b", "storage_tag": "chilled"}], "agent", at("2026-10-01", 9))
    names = dict(s.conn.execute("SELECT tag_id, name FROM product_tags").fetchall())
    fill = {names[k]: v for k, v in migration_fill(s.conn, date(2026, 10, 1)).items()}
    assert fill == {"jar a": 402, "jar b": 501}
    fill = {names[k]: v for k, v in migration_fill(s.conn, date(2026, 4, 1)).items()}
    assert fill == {"jar a": 401, "jar b": 501}, "inside the window on 1 Apr, the share decides (3 : 1)"


@pytest.mark.parametrize("name, sku", [("scenario", 202), pytest.param("recorded", 1316375, marks=recorded)])
def test_a_failed_migration_leaves_version_1_and_foreign_keys_on(tmp_path, name, sku):
    path = v1_db(tmp_path, name)
    raw = sqlite3.connect(path)
    raw.execute("UPDATE skus SET product_tag_id = 't9999' WHERE sku = ?", (sku,))  # a broken reference
    assert raw.total_changes == 1
    raw.commit()
    before = list(raw.iterdump())
    raw.close()
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(DbError, match="broken references"):
        migrate(conn, MIGRATED)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert list(conn.iterdump()) == before, "everything rolled back"
    cols = [r[1] for r in conn.execute("PRAGMA table_info(product_tags)")]
    assert "preferred_sku" not in cols
    conn.close()


def call(*argv) -> dict:
    env = cli._dispatch_safely(cli.build_parser().parse_args([str(a) for a in argv]))
    return json.loads(env.to_json())


@recorded
def test_after_the_migration_every_run_lists_the_solo_tags_until_a_non_dry_run_finishes(tmp_path, monkeypatch):
    """§8.1 section 5, §13.6 item 8: the next runs' "Check these tags" list every solo tag with its preferred
    product, dry runs included, until a non-dry run finishes normally; the plan buys the preferred products."""
    data = tmp_path / "data"
    v1_db(data / "1000001", "recorded")
    monkeypatch.setenv("ROHLIK_REPLENISH_DATA_DIR", str(data))
    run = ["run", "--fixtures", FIX, "--date", "2026-10-30"]
    solo_lines, envs = [], []
    for now, dry in (("2026-10-28T12:00:00+01:00", True), ("2026-10-28T12:30:00+01:00", False),
                     ("2026-10-29T13:00:00+01:00", False)):  # a day later: fixture carts reload empty (§6.6 lapse)
        env = call(*run, "--now", now, *(["--dry-run"] if dry else []))
        assert env["status"] == ("dry_run" if dry else "ok"), env["summary_md"][:400]
        envs.append(env)
        md = env["summary_md"]
        solo_lines.append([x for x in md.splitlines() if "(solo: prefers " in x])
    assert len(solo_lines[0]) == 19 and solo_lines[1] == solo_lines[0], "listed by the dry run and the first " \
                                                                       "normally finished non-dry run"
    assert solo_lines[2] == [], "the check ended"
    assert "- Chilled — eggs (solo: prefers Pohodová vejce Podestýlková L)" in solo_lines[0]
    assert env["summary_md"].count("Tell me if any is wrong") == 0
    conn = connect(str(data / "1000001" / "replenish.db"), MIGRATED)
    preferred = dict(conn.execute("SELECT name, preferred_sku FROM product_tags WHERE policy = 'solo'").fetchall())
    conn.close()
    rows = [r for r in envs[1]["data"]["plan"] if r["tag"] in preferred and r["reason"] in ("due", "stockup")]
    assert rows and all(r["sku"] == preferred[r["tag"]] or r["fallback_from"] == preferred[r["tag"]] for r in rows)


@recorded
def test_after_the_migration_the_users_own_preferred_products_are_not_listed_back(tmp_path, monkeypatch):
    """§8.1 section 5, §13.1: while `preferred_check` is set, every solo tag is listed except those whose
    preferred product the user named himself since. Here the migration runs at the first command that opens the
    database, `tags list` (§5.1); the next run still lists the others."""
    data = tmp_path / "data"
    v1_db(data / "1000001", "recorded")
    (data / "auth.json").write_text(json.dumps({"last_account_id": "1000001"}), encoding="utf-8")
    monkeypatch.setenv("ROHLIK_REPLENISH_DATA_DIR", str(data))
    fixed = ["--fixtures", FIX, "--now", "2026-10-28T12:00:00+01:00"]
    env = call("tags", "list", "--tag", "eggs", *fixed)
    assert env["status"] == "ok"
    assert "## eggs · solo, prefers Pohodová vejce Podestýlková L (1316375) (set by agent)" in env["summary_md"]
    env = call("tags", "set", "eggs", "--preferred", "1316375", "--source", "user", *fixed)  # confirms the pick
    assert env["status"] == "ok", env
    env = call("tags", "set", "cheddar", "--preferred", "1330721", "--source", "user", *fixed)  # names another
    assert env["status"] == "ok", env
    env = call("run", "--fixtures", FIX, "--date", "2026-10-30", "--now", "2026-10-28T12:30:00+01:00", "--dry-run")
    assert env["status"] == "dry_run", env["summary_md"][:400]
    listed = [x for x in env["summary_md"].splitlines() if "(solo: prefers " in x]
    assert len(listed) == 17, listed
    assert not any(" — eggs (" in x or " — cheddar (" in x for x in listed), "his own choices aren't listed back"
    assert "- Chilled — bacon sliced (solo: prefers Dacello Anglická slanina plátky 96 %)" in listed


# ------------------------------------------------------------------------------ the first run's guess (§6.13)
BREAD, A, B, C = 900, 901, 902, 903


def guess_household(tmp_path, sequence, prices, *, paid=None, claims=None, storage="chilled", extra=None, units=1):
    """Weekly orders of bread plus, per order, `units` of each yogurt in `sequence` (an int, a tuple of ints, or
    None), each 150 g; `paid` overrides the price paid per unit and `claims` claims whole lines."""
    s = Scenario(tmp_path)
    s.product(BREAD, "Bread 500 g", "500 g", 40.0)
    for sku, price in prices.items():
        s.product(sku, f"Yogurt {sku} 150 g", "150 g", price)
    day = date(2026, 4, 2)
    for item in sequence:
        lines = {BREAD: 1}
        for sku in (item if isinstance(item, tuple) else (item,) if item else ()):
            lines[sku] = units
        s.delivered(day.isoformat(), lines, prices=paid,
                    claims={sku: lines[sku] for sku in lines if sku in (claims or ())} or None)
        day += timedelta(days=7)
    mapping = {BREAD: ("bread", "cheapest", "kg", "bakery")}
    for sku in prices:
        mapping[sku] = ("yogurt", "cheapest", "kg", storage, dict(extra or {}))
    return s, mapping


NOW = at("2026-09-28", 9)


def first_run(s, mapping, source="agent"):
    env = s.run(NOW, date_="2026-09-30")
    assert env.status == "needs_tags", env.summary_md
    s.tag_all(mapping, NOW, source=source)
    env = s.run(NOW, date_="2026-09-30")
    assert env.status == "dry_run", env.summary_md
    return env


def stored(s, name="yogurt"):
    return tuple(s.conn.execute("SELECT policy, preferred_sku, preferred_source FROM product_tags WHERE name = ?",
                                (name,)).fetchone())


def test_the_guess_makes_a_pricier_favourite_over_40_percent_solo_and_stores_it(tmp_path):
    s, mapping = guess_household(tmp_path, [A, B, A, C, A, B, A, C, A, B], {A: 30.0, B: 25.0, C: 24.0})
    env = first_run(s, mapping)
    g = env.data["guessed_preferred"]["yogurt"]
    assert (g["sku"], g["purchases"], g["of"], g["pricier_pct"]) == (A, 5, 10, 22)  # 200 vs (166.7 + 160) / 2 CZK/kg
    assert stored(s) == ("solo", A, "agent"), "stored by the first run, a dry run"
    full = Path(env.data["report_path"]).read_text(encoding="utf-8")
    assert "- **yogurt** · solo: prefers Yogurt 901 150 g — 5 of 10 purchases, 22 % pricier than the others you " \
           "bought · kg" in full
    assert "- yogurt: solo, prefers Yogurt 901 150 g (5 of 10 purchases, 22 % pricier)" in env.summary_md
    rows = [r for r in env.data["plan"] if r["tag"] == "yogurt"]
    assert all(r["sku"] == A for r in rows if r["reason"] in ("due", "stockup")), "the first run plans with it"


@pytest.mark.parametrize("case", ["share_40", "favourite_cheapest", "favourite_unpaid", "no_other_paid",
                                  "one_product", "user_policy", "q_zero"])
def test_no_guess(tmp_path, case):
    # share_40: A and B tie at 4 of 10 (40 %), A bought last and pricier, so only the share stops the guess
    seq, prices, paid, claims, source = [B, A, B, A, C] * 2, {A: 30.0, B: 25.0, C: 24.0}, None, None, "agent"
    if case == "favourite_cheapest":
        seq, prices = [A, B, A, C, A, B, A, C, A, B], {A: 20.0, B: 25.0, C: 24.0}
    elif case == "favourite_unpaid":
        seq, paid = [A, B, A, C, A, B, A, C, A, B], {A: 0.0}
    elif case == "no_other_paid":
        seq, paid = [A, B, A, C, A, B, A, C, A, B], {B: 0.0, C: 0.0}
    elif case == "one_product":
        seq, prices = [A] * 10, {A: 30.0, B: 25.0}
    elif case == "user_policy":
        seq, source = [A, B, A, C, A, B, A, C, A, B], "user"
    elif case == "q_zero":
        seq, claims = [A, B, A, C, A, B, A, C, A, B], (A, B, C)
    s, mapping = guess_household(tmp_path, seq, prices, paid=paid, claims=claims)
    if case == "one_product":
        s.fake.set_cart_qty(B, 1)  # a second product, known from the cart but never bought
    env = first_run(s, mapping, source=source)
    assert env.data["guessed_preferred"] == {}, case
    assert stored(s) == ("cheapest", None, None), case


def test_a_partly_missing_price_only_shrinks_the_mean(tmp_path):
    s, mapping = guess_household(tmp_path, [A, B, A, C, A, B, A, C, A, B], {A: 30.0, B: 25.0, C: 24.0},
                                 paid={B: 0.0})
    env = first_run(s, mapping)
    assert env.data["guessed_preferred"]["yogurt"]["pricier_pct"] == 25, "200 against C's 160 CZK/kg alone"


def test_guess_ties_go_to_the_latest_purchase_then_the_lower_sku(tmp_path):
    s, mapping = guess_household(tmp_path, [B, A, C, B, A, B, A], {A: 30.0, B: 31.0, C: 24.0})
    env = first_run(s, mapping)
    assert env.data["guessed_preferred"]["yogurt"]["sku"] == A, "3 : 3, and A was bought last"
    s, mapping = guess_household(tmp_path / "2", [C, A, B, A, B, (A, B)], {A: 30.0, B: 31.0, C: 24.0})
    env = first_run(s, mapping)
    assert env.data["guessed_preferred"]["yogurt"]["sku"] == A, "3 : 3, last bought together: the lower id"


def test_no_guess_for_an_on_sale_mode_stored_or_assessed(tmp_path):
    s, mapping = guess_household(tmp_path, [A, B, A, C, A, B, A, C, A, B], {A: 30.0, B: 25.0, C: 24.0},
                                 storage="dry", extra={"stockup_mode": "on_sale"})
    env = first_run(s, mapping)
    assert env.data["guessed_preferred"] == {} and stored(s)[0] == "cheapest"
    # assessed this run: every purchase at least 35 % below today's regular price
    s, mapping = guess_household(tmp_path / "2", [A, B, A, C, A, B, A, C, A, B], {A: 30.0, B: 25.0, C: 24.0},
                                 storage="dry", paid={A: 15.0, B: 12.0, C: 11.0})
    env = first_run(s, mapping)
    assert env.data["assessed"] == {"yogurt": "on_sale"} and env.data["guessed_preferred"] == {}
    # the same purchases at the regular price: assessed always, and guessed
    s, mapping = guess_household(tmp_path / "3", [A, B, A, C, A, B, A, C, A, B], {A: 30.0, B: 25.0, C: 24.0},
                                 storage="dry")
    env = first_run(s, mapping)
    assert env.data["assessed"] == {"yogurt": "always"} and env.data["guessed_preferred"]["yogurt"]["sku"] == A


def test_a_tag_meeting_the_conditions_after_setup_is_not_guessed(tmp_path):
    s, mapping = guess_household(tmp_path, [A, None, B, None], {A: 30.0, B: 25.0})
    env = first_run(s, mapping)  # n = 2: no guess at setup
    assert env.data["guessed_preferred"] == {}
    for i, day in enumerate(("2026-10-01", "2026-10-08", "2026-10-15", "2026-10-22")):
        s.delivered(day, {BREAD: 1, A if i % 3 else B: 1})
    env = s.run(at("2026-10-26", 9), date_="2026-10-28")
    assert env.status == "ok" and env.data["guessed_preferred"] == {}
    assert stored(s) == ("cheapest", None, None)

# ------------------------------------------------------------------------------ solo: candidates and fallbacks
def test_a_preferred_product_never_bought_is_a_candidate(tmp_path):
    """§6.13: the preferred product is a candidate even if it was never bought (here it is known from the cart)."""
    s, mapping = guess_household(tmp_path, [B, C] * 6, {A: 30.0, B: 25.0, C: 24.0}, units=4)
    s.fake.set_cart_qty(A, 1)
    mapping = {**mapping, A: ("yogurt", "solo", "kg", "chilled", {"preferred": True}),
               B: ("yogurt", "solo", "kg", "chilled"), C: ("yogurt", "solo", "kg", "chilled")}
    env = first_run(s, mapping)
    rows = [r for r in env.data["plan"] if r["tag"] == "yogurt"]
    assert rows and rows[0]["sku"] == A and rows[0]["fallback_from"] is None, rows


def butter_streak(tmp_path):
    from tests.test_variants import MADETA, PILOS, cheese_household

    s = cheese_household(tmp_path)  # butter is solo and prefers Madeta; set up on 28 Sep
    s.set_price(MADETA, status="UNAVAILABLE")
    s.fake.products[MADETA]["stock"]["availabilityReason"] = "Vyprodáno."
    return s, MADETA, PILOS


REMINDER = "butter: Madeta butter 250 g unavailable (Vyprodáno.) — Pilos butter 250 g instead; name another to change it"


def test_a_fallback_reminder_shows_from_the_streak_start_in_3_finished_runs_then_only_in_status(tmp_path):
    """§2, §6.13: the moment is the start of the streak's first run; 3 normally finished non-dry runs show it (a dry
    run shows it but doesn't count); then only `status` lists it. Buying the preferred product again ends the streak,
    and a new one shows again."""
    s, madeta, pilos = butter_streak(tmp_path)
    shown, rows = [], []
    start = at("2026-09-29", 10)
    for i, dry in enumerate((False, True, False, False, False)):
        env = s.run(start + timedelta(minutes=10 * i), date_="2026-10-01", dry=dry)
        assert env.status == ("dry_run" if dry else "ok"), env.summary_md
        shown.append(REMINDER in env.summary_md)
        rows.append([r for r in env.data["plan"] if r["tag"] == "butter"])
    assert shown == [True, True, True, True, False]
    assert all(r and r[0]["sku"] == pilos and r[0]["fallback_from"] == madeta for r in rows)
    stored = s.conn.execute("SELECT COUNT(*) FROM run_plan WHERE sku = ? AND fallback_from = ?",
                            (pilos, madeta)).fetchone()[0]
    assert stored == 5, "every run's row records the preferred SKU"
    md, data = status(s.conn, start + timedelta(hours=1))
    assert data["solo_fallbacks"] == ["butter (Madeta butter 250 g unavailable, buying Pilos butter 250 g)"]
    # the preferred product is back: bought again, and the streak ends
    s.set_price(madeta, status="AVAILABLE")
    env = s.run(start + timedelta(hours=2), date_="2026-10-01")
    butter = [r for r in env.data["plan"] if r["tag"] == "butter"]
    assert butter[0]["sku"] == madeta and butter[0]["fallback_from"] is None and REMINDER not in env.summary_md
    assert s.cart().get(madeta) and not s.cart().get(pilos)
    # sold out again: a new streak, shown again
    s.set_price(madeta, status="UNAVAILABLE")
    env = s.run(start + timedelta(hours=3), date_="2026-10-01")
    assert REMINDER in env.summary_md
    md, data = status(s.conn, start + timedelta(hours=4))
    assert data["solo_fallbacks"], "status always lists it"


def test_a_fallback_reminder_counts_only_the_runs_that_show_it(tmp_path):
    """§2, §13.1: the 3 showings are the first 3 normally finished runs that buy the tag without its preferred
    product. A back-to-back re-run that keeps the fallback line shows it and counts; runs that don't buy the tag (here
    for the weeks the user's own 3 packs last) neither show it nor use it up, so the next substitution is mentioned."""
    s, madeta, pilos = butter_streak(tmp_path)
    log = []  # per finished non-dry run: (it bought butter without its preferred product, the reminder showed)
    now = at("2026-09-29", 10)
    for week in range(8):
        t = (now + timedelta(days=2)).date().isoformat()
        for rerun in range(2 if week == 0 else 1):
            env = s.run(now + timedelta(minutes=10 * rerun), date_=t)
            assert env.status == "ok", env.summary_md
            fallback = [r for r in env.data["plan"] if r["tag"] == "butter" and r["fallback_from"] == madeta]
            log.append((bool(fallback), REMINDER in env.summary_md))
        if week == 0:
            assert s.fake.writes and s.cart().get(pilos) == 1
            s.fake.set_cart_qty(pilos, 3)  # he takes 3 of the fallback: butter isn't needed for a few weeks
        oid = s.checkout(now + timedelta(minutes=30), t)
        s.deliver(oid)
        now += timedelta(days=7)
    assert log[:2] == [(True, True), (True, True)], "the re-run keeps the line, shows it and counts"
    gap = [i for i, (bought, _) in enumerate(log) if not bought]
    assert gap and gap[0] == 2, log
    assert all(not shown for bought, shown in log if not bought), "a run that doesn't buy the tag doesn't show it"
    showings = [shown for bought, shown in log if bought]
    assert len(showings) >= 4, log
    assert showings[:3] == [True, True, True] and not any(showings[3:]), log


def test_an_on_sale_solo_tag_without_its_preferred_product_is_unavailable_with_its_availability(tmp_path):
    s = Scenario(tmp_path)
    s.product(BREAD, "Bread 500 g", "500 g", 40.0)
    s.product(301, "Coffee Alto 250 g", "250 g", 149.0)
    s.product(302, "Coffee Basso 250 g", "250 g", 99.0)
    day = date(2026, 4, 2)
    for i in range(24):
        s.delivered(day.isoformat(), {BREAD: 1, **({301: 1} if i % 2 == 0 else {}), **({302: 1} if i in (5, 9) else {})})
        day += timedelta(days=7)
    s.set_price(301, status="UNAVAILABLE")
    s.fake.products[301]["stock"]["availabilityReason"] = "Vyprodáno."
    mapping = {BREAD: ("bread", "cheapest", "kg", "bakery"),
               301: ("coffee", "solo", "kg", "dry", {"preferred": True, "stockup_mode": "on_sale"}),
               302: ("coffee", "solo", "kg", "dry")}
    env = first_run(s, mapping)
    env = s.run(at("2026-10-01", 9), date_="2026-10-03")
    coffee = [r for r in env.data["plan"] if r["tag"] == "coffee"]
    assert coffee and coffee[0]["reason"] == "unavailable" and coffee[0]["fallback_from"] == 301, s.dump(env)
    assert "- **coffee** (Coffee Alto 250 g): unavailable: Vyprodáno." in env.summary_md
    assert "coffee: Coffee Alto 250 g unavailable (Vyprodáno.); name another to change it" in env.summary_md
    assert not s.cart().get(301) and not s.cart().get(302), "no fallback for an on_sale solo tag"


def test_a_guessed_preferred_product_stays_through_purchases_and_taggings(tmp_path):
    """§13.1: it is unavailable for weeks, fallbacks make another product the most bought, a new SKU joins the tag,
    and when it returns it is bought again."""
    s, mapping = guess_household(tmp_path, [A, B, A, C, A, B, A, C, A, B, A, B], {A: 30.0, B: 25.0, C: 24.0})
    env = first_run(s, mapping)
    assert stored(s) == ("solo", A, "agent")
    s.set_price(A, status="UNAVAILABLE")
    now = at("2026-09-29", 10)
    def window_purchases(sku):
        start = (now.date() - timedelta(days=183)).isoformat()
        return s.conn.execute("SELECT COUNT(*) FROM order_lines l JOIN orders o USING (order_id) WHERE l.sku = ? "
                              "AND o.delivery_date > ? AND l.quantity > 0", (sku, start)).fetchone()[0]

    for week in range(16):  # the fallback is the cheapest of the others, C
        t = (now + timedelta(days=2)).date().isoformat()
        env = s.run(now, date_=t)
        assert env.status == "ok", env.summary_md
        assert not s.cart().get(A)
        oid = s.checkout(now + timedelta(minutes=30), t)
        s.deliver(oid)
        now += timedelta(days=7)
        if window_purchases(C) > window_purchases(A) + 1:
            break
    assert window_purchases(C) > window_purchases(A) + 1, "the fallback is now the most bought"
    s.product(904, "Yogurt 904 150 g", "150 g", 22.0)
    s.fake.set_cart_qty(904, 1)
    env = s.run(now, date_=(now + timedelta(days=2)).date().isoformat())
    assert env.status == "needs_tags"
    tags_apply(s.conn, [{"sku": 904, "product_tag": "yogurt", "storage_tag": "chilled"}], "agent", now)
    s.fake.set_cart_qty(904, 0)
    s.set_price(A, status="AVAILABLE")
    env = s.run(now + timedelta(minutes=5), date_=(now + timedelta(days=2)).date().isoformat())
    assert env.status == "ok", env.summary_md
    yogurt = [r for r in env.data["plan"] if r["tag"] == "yogurt" and r["reason"] == "due"]
    assert yogurt and yogurt[0]["sku"] == A and yogurt[0]["fallback_from"] is None
    assert stored(s) == ("solo", A, "agent"), "nothing but the agent (for a new tag) or the user moves it"

# ------------------------------------------------------------------------------ tag commands (§5.3, §7.3, §9)
EGG_F, EGG_C, EGG_M, BUT_A, BUT_B, BUT_C, MILK = 1, 2, 3, 4, 5, 7, 6
T = at("2026-09-28", 10)


def tag_household(tmp_path):
    s = Scenario(tmp_path)
    for sku, name, text in ((EGG_F, "Eggs L Farm 10 ks", "10 ks"), (EGG_C, "Eggs L Coop 10 ks", "10 ks"),
                            (EGG_M, "Eggs M 10 ks", "10 ks"), (BUT_A, "Butter A 250 g", "250 g"),
                            (BUT_B, "Butter B 250 g", "250 g"), (BUT_C, "Butter C 250 g", "250 g"),
                            (MILK, "Milk 1 l", "1 l")):
        s.product(sku, name, text, 50.0)
    s.history("2026-06-01", 7, 12, {sku: 1 for sku in (EGG_F, EGG_C, EGG_M, BUT_A, BUT_B, BUT_C, MILK)})
    assert s.run(at("2026-09-28", 9), date_="2026-09-30").status == "needs_tags"
    tags_apply(s.conn, [
        {"sku": EGG_F, "product_tag": "eggs", "policy": "cheapest", "base_unit": "pcs", "storage_tag": "chilled"},
        {"sku": EGG_C, "product_tag": "eggs", "storage_tag": "chilled"},
        {"sku": EGG_M, "product_tag": "eggs", "storage_tag": "chilled"},
        {"sku": BUT_A, "product_tag": "butter", "policy": "solo", "base_unit": "kg", "storage_tag": "chilled",
         "preferred": True},
        {"sku": BUT_B, "product_tag": "butter", "storage_tag": "chilled"},
        {"sku": BUT_C, "product_tag": "butter", "storage_tag": "chilled"},
        {"sku": MILK, "product_tag": "milk", "policy": "cheapest", "base_unit": "l", "storage_tag": "chilled"}],
        "agent", T)
    return s


def tag(s, name):
    return tuple(s.conn.execute("SELECT policy, preferred_sku, preferred_source FROM product_tags WHERE name = ?",
                                (name,)).fetchone() or ())


def assigned(s, sku):
    return s.conn.execute("SELECT assigned_by FROM skus WHERE sku = ?", (sku,)).fetchone()[0]


def rejected(fn, *args, match: str, **kwargs):
    before = None
    conn = args[0]
    before = list(conn.iterdump())
    with pytest.raises(TagError, match=match):
        fn(*args, **kwargs)
    assert list(conn.iterdump()) == before, "a rejected command changes nothing"


def test_tags_set_preferred_makes_a_tag_solo_with_the_policy_rules(tmp_path):
    s = tag_household(tmp_path)
    assert tag(s, "butter") == ("solo", BUT_A, "agent") and tag(s, "eggs") == ("cheapest", None, None)
    rejected(tags_set, s.conn, "eggs", preferred=EGG_F, source="agent", now=T, match="only the user")
    rejected(tags_set, s.conn, "eggs", policy="solo", source="user", now=T, match="needs --preferred")
    rejected(tags_set, s.conn, "eggs", policy="cheapest", preferred=EGG_F, source="user", now=T,
             match="can't go with --policy cheapest")
    rejected(tags_set, s.conn, "eggs", preferred=MILK, source="user", now=T, match="isn't in 'eggs'")
    res = tags_set(s.conn, "eggs", preferred=EGG_F, source="user", now=T)
    assert tag(s, "eggs") == ("solo", EGG_F, "user") and assigned(s, EGG_F) == "user", "the user's from now on"
    assert "set policy solo, preferred product Eggs L Farm 10 ks (1)" in res.messages[0]
    tags_set(s.conn, "eggs", policy="solo", source="user", now=T)  # already solo: kept
    assert tag(s, "eggs") == ("solo", EGG_F, "user")
    res = tags_set(s.conn, "butter", policy="cheapest", source="agent", now=T)  # the agent's own: cleared
    assert tag(s, "butter") == ("cheapest", None, None)
    assert any("is cheapest now: Butter A 250 g (4) is no longer its preferred product" in m for m in res.messages)
    rejected(tags_set, s.conn, "eggs", policy="cheapest", source="agent", now=T, match="the user chose")
    res = tags_set(s.conn, "eggs", policy="variety", source="user", now=T)  # the user may, and is told
    assert tag(s, "eggs") == ("variety", None, None)
    assert any("no longer its preferred product" in m for m in res.messages)
    res = tags_set(s.conn, "eggs", preferred=EGG_C, source="user", now=T)
    assert tag(s, "eggs") == ("solo", EGG_C, "user") and any("its mix ends" in m for m in res.messages)
    md = tags_list(s.conn, "eggs")
    assert "## eggs · solo, prefers Eggs L Coop 10 ks (2) (set by user)" in md and "(2) · **preferred**" in md


def test_tags_apply_preferred_for_new_and_existing_tags(tmp_path):
    s = tag_household(tmp_path)
    new = {"product_tag": "eggs l", "policy": "solo", "base_unit": "pcs", "storage_tag": "chilled"}
    rejected(tags_apply, s.conn, [{"sku": EGG_F, **new}, {"sku": EGG_C, "product_tag": "eggs l",
                                                         "storage_tag": "chilled"}], "agent", T,
             match="needs exactly one entry with preferred: true \\(it has 0\\)")
    rejected(tags_apply, s.conn, [{"sku": EGG_F, **new, "preferred": True},
                                  {"sku": EGG_C, "product_tag": "eggs l", "storage_tag": "chilled", "preferred": True}],
             "agent", T, match="it has 2")
    rejected(tags_apply, s.conn, [{"sku": EGG_F, **new, "preferred": True}, {"sku": EGG_C, "ignore": True,
                                                                             "preferred": True}],
             "agent", T, match="an ignored product can't be")
    rejected(tags_apply, s.conn, [{"sku": EGG_F, **new, "policy": "cheapest", "preferred": True}], "agent", T,
             match="marks the product a solo tag buys")
    rejected(tags_apply, s.conn, [{"sku": EGG_F, **new, "preferred": "yes"}], "agent", T, match="true or false")
    tags_apply(s.conn, [{"sku": EGG_F, **new, "preferred": True},
                        {"sku": EGG_C, "product_tag": "eggs l", "storage_tag": "chilled"}], "agent", T)
    assert tag(s, "eggs l") == ("solo", EGG_F, "agent") and assigned(s, EGG_F) == "agent"
    # an existing tag: the agent can't name another; the user can, and his choice makes the SKU his
    rejected(tags_apply, s.conn, [{"sku": EGG_C, "product_tag": "eggs l", "storage_tag": "chilled",
                                   "preferred": True}], "agent", T, match="only the user names")
    rejected(tags_apply, s.conn, [{"sku": MILK, "product_tag": "milk", "storage_tag": "chilled", "preferred": True}],
             "user", T, match="make it solo with tags set")
    tags_apply(s.conn, [{"sku": EGG_C, "product_tag": "eggs l", "storage_tag": "chilled", "preferred": True}],
               "user", T)
    assert tag(s, "eggs l") == ("solo", EGG_C, "user") and assigned(s, EGG_C) == "user"
    # a later agent tagging adds a product without touching the preferred one
    tags_apply(s.conn, [{"sku": EGG_M, "product_tag": "eggs l", "storage_tag": "chilled"}], "agent", T)
    assert tag(s, "eggs l") == ("solo", EGG_C, "user")


def test_a_user_set_preferred_product_is_protected_judged_on_the_end_state(tmp_path):
    s = tag_household(tmp_path)
    tags_set(s.conn, "butter", preferred=BUT_A, source="user", now=T)
    assert tag(s, "butter") == ("solo", BUT_A, "user") and assigned(s, BUT_A) == "user"
    rejected(tags_apply, s.conn, [{"sku": BUT_A, "product_tag": "milk", "storage_tag": "chilled",
                                   "units_per_pack": 0.25}], "agent", T, match="assigned by the user")
    rejected(tags_apply, s.conn, [{"sku": BUT_A, "ignore": True}], "agent", T, match="assigned by the user")
    rejected(tags_set, s.conn, "butter", policy="assorted", source="agent", now=T, match="the user chose")
    rejected(tags_merge, s.conn, "butter", "milk", "agent", T, match="assigned by the user")
    # the end state keeps it: the agent may move another product out
    tags_apply(s.conn, [{"sku": BUT_C, "product_tag": "butter other", "policy": "cheapest", "base_unit": "kg",
                         "storage_tag": "chilled"}], "agent", T)
    assert tag(s, "butter") == ("solo", BUT_A, "user")
    # one user command moves the preferred product out and names another: judged on its end state, the tag stays
    # solo (entry by entry it would first have turned cheapest)
    res = tags_apply(s.conn, [{"sku": BUT_A, "product_tag": "butter other", "storage_tag": "chilled"},
                              {"sku": BUT_B, "product_tag": "butter", "storage_tag": "chilled", "preferred": True}],
                     "user", T)
    assert tag(s, "butter") == ("solo", BUT_B, "user")
    assert not any("now buys the cheapest" in m for m in res.messages)
    assert any("'butter': preferred product is now Butter B 250 g (5) (was Butter A 250 g (4))" in m
               for m in res.messages)


def test_the_preferred_sku_leaving_its_tag_turns_it_cheapest_with_a_report(tmp_path):
    s = tag_household(tmp_path)
    res = tags_apply(s.conn, [{"sku": BUT_A, "product_tag": "butter a", "policy": "cheapest", "base_unit": "kg",
                               "storage_tag": "chilled"}], "agent", T)  # the agent's own choice: allowed
    assert tag(s, "butter") == ("cheapest", None, None)
    assert "butter now buys the cheapest: its preferred product moved; name another to keep it `solo`" in \
        res.messages
    tags_set(s.conn, "eggs", preferred=EGG_F, source="user", now=T)
    res = tags_apply(s.conn, [{"sku": EGG_F, "ignore": True}], "user", T)  # the user may, and is told
    assert tag(s, "eggs") == ("cheapest", None, None)
    assert "eggs now buys the cheapest: its preferred product moved; name another to keep it `solo`" in res.messages


@pytest.mark.parametrize("source, preset", [("agent", "agent"), ("user", "user"), ("user", "agent")])
def test_a_split_into_a_new_solo_tag_that_prefers_the_same_product_still_reports(tmp_path, source, preset):
    """§5.3, §13.1: the preferred SKU leaves its tag also when it becomes another tag's preferred product. The
    old tag keeps its other products, turns cheapest, and says so."""
    s = tag_household(tmp_path)
    if preset == "user":
        tags_set(s.conn, "butter", preferred=BUT_A, source="user", now=T)
    assert tag(s, "butter") == ("solo", BUT_A, preset)
    res = tags_apply(s.conn, [{"sku": BUT_A, "product_tag": "butter salted", "policy": "solo", "base_unit": "kg",
                               "storage_tag": "chilled", "preferred": True}], source, T)
    assert tag(s, "butter salted") == ("solo", BUT_A, source)
    assert tag(s, "butter") == ("cheapest", None, None)
    siblings = s.conn.execute("SELECT s.sku FROM skus s JOIN product_tags t ON t.tag_id = s.product_tag_id "
                              "WHERE t.name = 'butter' ORDER BY s.sku").fetchall()
    assert [r[0] for r in siblings] == [BUT_B, BUT_C], "the siblings stay in the old tag"
    assert "butter now buys the cheapest: its preferred product moved; name another to keep it `solo`" in \
        res.messages, res.messages


def test_two_tags_swapping_their_preferred_products_in_one_command_report_both(tmp_path):
    """§13.1: each tag's preferred product changed, although both SKUs are still somebody's preferred."""
    s = tag_household(tmp_path)
    tags_apply(s.conn, [{"sku": BUT_B, "product_tag": "butter b", "policy": "solo", "base_unit": "kg",
                         "storage_tag": "chilled", "preferred": True}], "agent", T)
    assert tag(s, "butter") == ("solo", BUT_A, "agent") and tag(s, "butter b") == ("solo", BUT_B, "agent")
    res = tags_apply(s.conn, [
        {"sku": BUT_A, "product_tag": "butter b", "storage_tag": "chilled", "preferred": True},
        {"sku": BUT_B, "product_tag": "butter", "storage_tag": "chilled", "preferred": True}], "user", T)
    assert tag(s, "butter") == ("solo", BUT_B, "user") and tag(s, "butter b") == ("solo", BUT_A, "user")
    assert "'butter': preferred product is now Butter B 250 g (5) (was Butter A 250 g (4))" in res.messages
    assert "'butter b': preferred product is now Butter A 250 g (4) (was Butter B 250 g (5))" in res.messages
    assert not any("now buys the cheapest" in m for m in res.messages)


def test_merge_keeps_a_user_set_preferred_product_and_reports_every_drop(tmp_path):
    s = tag_household(tmp_path)
    tags_apply(s.conn, [{"sku": EGG_F, "product_tag": "eggs farm", "policy": "solo", "base_unit": "pcs",
                         "storage_tag": "chilled", "preferred": True}], "user", T)
    tags_set(s.conn, "eggs", preferred=EGG_C, source="user", now=T)
    tags_set(s.conn, "eggs", policy="cheapest", source="user", now=T)
    tags_apply(s.conn, [{"sku": EGG_C, "product_tag": "eggs coop", "policy": "solo", "base_unit": "pcs",
                         "storage_tag": "chilled", "preferred": True}], "user", T)
    tags_set(s.conn, "eggs coop", policy="cheapest", source="user", now=T)
    tags_apply(s.conn, [{"sku": EGG_C, "product_tag": "eggs coop", "storage_tag": "chilled"}], "user", T)
    # INTO (butter) has an agent-set preferred product; FROM (eggs farm) a user-set one: FROM's wins
    res = tags_merge(s.conn, "eggs farm", "butter", "user", T)
    assert tag(s, "butter") == ("solo", EGG_F, "user")
    assert any("'butter': preferred product is now Eggs L Farm 10 ks (1) (was Butter A 250 g (4))" in m
               for m in res.messages)
    assert not any("'eggs farm' is gone" in m for m in res.messages), "adopted by INTO: nothing was dropped"
    # INTO keeps its user-set one; FROM's agent-set one is dropped and reported
    tags_apply(s.conn, [{"sku": EGG_M, "product_tag": "eggs m", "policy": "solo", "base_unit": "pcs",
                         "storage_tag": "chilled", "preferred": True}], "agent", T)
    res = tags_merge(s.conn, "eggs m", "butter", "agent", T)
    assert tag(s, "butter") == ("solo", EGG_F, "user")
    assert any("'eggs m' is gone, and with it its preferred product Eggs M 10 ks (3)" in m for m in res.messages)
    # INTO without one (cheapest) keeps its policy: FROM's user-set product is dropped, the user is told
    tags_apply(s.conn, [{"sku": BUT_B, "product_tag": "butter b", "policy": "solo", "base_unit": "kg",
                         "storage_tag": "chilled", "preferred": True}], "user", T)
    rejected(tags_merge, s.conn, "butter b", "milk", "agent", T, match="assigned by the user")
    res = tags_merge(s.conn, "butter b", "eggs coop", "user", T)
    assert tag(s, "eggs coop") == ("cheapest", None, None)
    assert any("'butter b' is gone, and with it its preferred product Butter B 250 g (5)" in m for m in res.messages)


# ------------------------------------------------------------------------------ outputs (§7.1 step 6, §9)
PASTA, SAUCE, SOAP, CHIPS = 11, 12, 13, 14


def test_a_first_runs_needs_tags_lists_co_purchases_with_untagged_products_and_differing_texts(tmp_path):
    s = Scenario(tmp_path)
    s.product(PASTA, "Pasta 500 g", "500 g", 30.0)
    s.product(SAUCE, "Tomato sauce 400 g", "400 g", 45.0)
    s.product(SOAP, "Dish soap 2 l", "2 l", 60.0)
    s.product(CHIPS, "Chips 150 g", "150 g", 40.0)
    for day in ("2026-08-03", "2026-08-10", "2026-08-17"):
        s.delivered(day, {PASTA: 1, SAUCE: 1, SOAP: 1})
    s.delivered("2026-08-24", {PASTA: 1, CHIPS: 1})  # bought together once: not listed
    s.fake.products[SOAP]["textualAmount"] = "500 ml"  # the pack changed
    env = s.run(at("2026-09-28", 9), date_="2026-09-30")
    assert env.status == "needs_tags"
    items = {x["sku"]: x for x in env.data["skus"]}
    assert items[PASTA]["bought_with"] == ["Dish soap 2 l (3x)", "Tomato sauce 400 g (3x)"], "untagged, by name"
    assert items[SOAP]["text"] == "500 ml" and items[SOAP]["line_texts"] == "'2 l' in 3 orders, '500 ml' now"
    assert "line_texts" not in items[PASTA]
    assert "order-line texts: '2 l' in 3 orders, '500 ml' now" in env.summary_md


def test_command_summaries_name_at_most_10_items(tmp_path):
    s = Scenario(tmp_path)
    for i in range(12):
        s.product(100 + i, f"Product {i:02d} 1 kg", "1 kg", 10.0)
    s.history("2026-08-03", 7, 4, {100 + i: 1 for i in range(12)})
    s.run(at("2026-09-28", 9), date_="2026-09-30")
    res = tags_apply(s.conn, [{"sku": 100 + i, "product_tag": f"tag {i:02d}", "policy": "cheapest", "base_unit": "kg",
                               "storage_tag": "dry"} for i in range(12)], "agent", T)
    assert res.messages[0].endswith("tag 09 (cheapest, kg) and 2 more") and "tag 10" not in res.messages[0]
    assert len(res.data["created"]) == 12, "data keeps the full list"


def test_a_product_sharing_its_name_with_another_in_its_tag_gets_its_pack_text(tmp_path):
    """§8.1: names as in Rohlik, followed by the pack text where two products of a tag share a name."""
    s = Scenario(tmp_path)
    s.product(BREAD, "Bread 500 g", "500 g", 40.0)
    s.product(21, "Salmon sliced", "200 g", 80.0)  # the cheaper per kg, so neither guessed solo nor passed over
    s.product(22, "Salmon sliced", "100 g", 45.0)
    day = date(2026, 4, 2)
    for i in range(24):
        s.delivered(day.isoformat(), {BREAD: 1, (21 if i % 2 else 22): 1})
        day += timedelta(days=7)
    mapping = {BREAD: ("bread", "cheapest", "kg", "bakery"), 21: ("smoked salmon", "cheapest", "kg", "chilled"),
               22: ("smoked salmon", "cheapest", "kg", "chilled")}
    first_run(s, mapping)
    env = s.run(at("2026-09-28", 10), date_="2026-09-30", dry=True)
    adds = env.summary_md.split("### Would add\n")[1].split("\n\n")[0].splitlines()
    assert any(x.startswith("- Salmon sliced (200 g) ×") for x in adds), adds
    assert any(x.startswith("- Bread 500 g ×") for x in adds), "a name no other product of its tag has stays bare"
    assert {r["name"] for r in env.data["plan"] if r["tag"] == "smoked salmon"} == {"Salmon sliced"}, \
        "data keeps the names as in Rohlik"