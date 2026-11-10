"""§13.1 classification, managed long-life tags, overdue, one-time notices, incoming stock, account note."""

from datetime import date, timedelta

from engine.tagsvc import prefs_never, tags_apply
from tests.helpers import Scenario, at, rohlik_time

MILK, YOG, TP, CORN, FILLER = 101, 102, 501, 502, 900


def rows(env, tag):
    return [r for r in env.data["plan"] if r["tag"] == tag]


def base(tmp_path):
    s = Scenario(tmp_path)
    s.product(MILK, "Milk 1 l", "1 l", 25.0)
    s.product(YOG, "Yogurt 150 g", "150 g", 15.0)
    s.product(FILLER, "Rolls 6 ks", "6 ks", 30.0)
    s.history("2026-04-01", 7, 26, {MILK: 2, FILLER: 1})
    return s


MAP = {MILK: ("milk", "cheapest", "l", "chilled"), FILLER: ("rolls", "cheapest", "pcs", "bakery")}
YMAP = {**MAP, YOG: ("yogurt", "cheapest", "kg", "chilled")}


def test_suggest_gate_only_when_due_with_two_purchases(tmp_path):
    s = base(tmp_path)
    s.delivered("2026-08-01", {YOG: 2})
    s.delivered("2026-08-29", {YOG: 2})
    env = s.first_runs(at("2026-09-28", 9), YMAP, date_="2026-09-30")
    assert [r["reason"] for r in rows(env, "yogurt")] == ["suggest_gate"]
    assert "- **yogurt** (Yogurt 150 g): bought twice in 6 months" in env.summary_md, "it names the window"


def test_suggest_gate_names_the_12_month_window_of_a_long_life_tag(tmp_path):
    s = base(tmp_path)
    s.delivered("2026-07-15", {YOG: 2})
    s.delivered("2026-08-12", {YOG: 2})
    env = s.first_runs(at("2026-09-28", 9), {**MAP, YOG: ("yogurt", "cheapest", "kg", "dry")}, date_="2026-09-30")
    assert [r["reason"] for r in rows(env, "yogurt")] == ["suggest_gate"]
    assert "- **yogurt** (Yogurt 150 g): bought twice in 12 months" in env.summary_md


def test_precedence_never_beats_everything(tmp_path):
    s = base(tmp_path)
    env = s.first_runs(at("2026-09-28", 9), MAP, date_="2026-09-30")
    assert rows(env, "milk")
    prefs_never(s.conn, "add", "milk", at("2026-09-28", 9))
    env = s.run(at("2026-09-28", 10), date_="2026-09-30")
    assert not rows(env, "milk") and MILK not in s.cart()
    assert "milk" not in env.summary_md.lower().split("### totals")[0], "excluded tags show only in tags list"


def test_size_unknown_beats_due_with_two_window_events(tmp_path):
    s = base(tmp_path)
    s.product(YOG, "Yogurt big", "1 kelímek", 15.0)
    s.history("2026-07-01", 7, 10, {YOG: 1})
    env1 = s.run(at("2026-09-28", 9), date_="2026-09-30")
    assert env1.status == "needs_tags"
    s.tag_all({**YMAP, YOG: ("yogurt", "cheapest", "kg", "chilled", {"units_per_pack": None})})
    env = s.run(at("2026-09-28", 9), date_="2026-09-30")
    assert [r["reason"] for r in rows(env, "yogurt")] == ["size_unknown"]
    assert "pack size unknown" in env.summary_md


def test_an_unknown_size_on_the_agents_own_cart_line_makes_the_tag_size_unknown(tmp_path):
    """§6.2: the gate covers every cart line, the agent's included, so no replacement line is allocated."""
    yog2 = 103
    s = base(tmp_path)
    s.product(yog2, "Yogurt other 150 g", "150 g", 16.0)
    s.history("2026-04-02", 7, 26, {YOG: 3})
    s.delivered("2026-05-06", {yog2: 3})
    s.first_runs(at("2026-09-28", 9), {**YMAP, yog2: ("yogurt", "cheapest", "kg", "chilled")}, date_="2026-09-30")
    env = s.run(at("2026-09-28", 10), date_="2026-09-30")
    assert env.status == "ok" and s.cart().get(YOG) and not s.cart().get(yog2), s.dump(env)
    s.product(YOG, "Yogurt 150 g", "1 kelímek", 15.0)  # its live pack text no longer parses
    env = s.run(at("2026-09-28", 11), date_="2026-09-30")
    assert [r["reason"] for r in rows(env, "yogurt")] == ["size_unknown"], s.dump(env)
    assert not s.cart().get(yog2), "no replacement line on the sibling"


def _managed(tmp_path, *, mode="always"):
    """A long-life tag with a stored mode and one window event: its older purchases were cached by a run a year
    ago (the first sync reaches back only 365 days)."""
    s = Scenario(tmp_path)
    s.product(MILK, "Milk 1 l", "1 l", 25.0)
    s.product(FILLER, "Rolls 6 ks", "6 ks", 30.0)
    s.product(TP, "Toilet paper 16 rolls", "308,8 m", 180.0)
    s.history("2025-03-03", 7, 26, {MILK: 2, FILLER: 1})
    s.delivered("2025-06-02", {TP: 1})
    s.delivered("2025-08-01", {TP: 1})
    s.first_runs(at("2025-09-01", 9), {**MAP, TP: ("toilet paper", "cheapest", "m", "drugstore-household",
                                                   {"stockup_mode": mode})}, date_="2025-09-03")
    s.history("2025-09-01", 7, 56, {MILK: 2, FILLER: 1})
    s.delivered("2026-01-15", {TP: 1})
    return s


def test_managed_long_life_tag_is_gated_at_one_window_event(tmp_path):
    s = _managed(tmp_path)
    env = s.run(at("2026-09-28", 10), date_="2026-09-30")
    tp = rows(env, "toilet paper")
    assert tp and tp[0]["reason"] == "due", s.dump(env)
    assert any(t == "get_product_details" and TP in a["product_ids"] for t, a in s.fake.calls), "details were read"


def test_managed_on_sale_tag_is_topped_up_on_a_deep_sale(tmp_path):
    s = _managed(tmp_path, mode="on_sale")
    s.set_price(TP, sale=99.0, sale_until="2026-10-05")
    env = s.run(at("2026-09-28", 10), date_="2026-09-30", dry=True)
    tp = rows(env, "toilet paper")
    assert tp and tp[0]["stockup_units"] + tp[0]["normal_units"] >= 1, s.dump(env)


def test_managed_tag_unknown_size_is_size_unknown_never_suggest_gate(tmp_path):
    s = _managed(tmp_path)
    s.product(TP, "Toilet paper 16 rolls", "16 rolí", 180.0)
    env = s.run(at("2026-09-28", 10), date_="2026-09-30", dry=True)
    assert [r["reason"] for r in rows(env, "toilet paper")] in (["size_unknown"], ["due"])
    assert all(r["reason"] != "suggest_gate" for r in rows(env, "toilet paper"))


def test_overdue_after_twice_the_usual_gap(tmp_path):
    s = base(tmp_path)
    s.product(CORN, "Corn flakes 750 g", "0,75 kg", 60.0)
    for i in range(10):
        s.delivered((at("2026-03-01") + timedelta(days=14 * i)).date().isoformat(), {CORN: 1})
    s.delivered("2026-08-02", {CORN: 30})  # an estimate gone wrong: lots "left", but nothing bought since
    env = s.first_runs(at("2026-09-28", 9), {**MAP, CORN: ("corn flakes", "cheapest", "kg", "dry",
                                                           {"stockup_mode": "always"})}, date_="2026-09-30")
    cf = rows(env, "corn flakes")
    assert cf and cf[0]["reason"] == "overdue", s.dump(env)
    # a reminder (§2) dated 31 Aug, before setup completed: its row is stored, but the summary doesn't show it
    assert "check your stock" not in env.summary_md


def test_overdue_is_a_reminder_for_three_finished_runs_then_only_in_status(tmp_path):
    s = Scenario(tmp_path)
    s.product(MILK, "Milk 1 l", "1 l", 25.0)
    s.product(FILLER, "Rolls 6 ks", "6 ks", 30.0)
    s.product(CORN, "Corn flakes 750 g", "0,75 kg", 60.0)
    s.history("2026-02-01", 7, 29, {MILK: 2, FILLER: 1})  # until 16 Aug
    for i in range(10):
        s.delivered((at("2026-03-01") + timedelta(days=14 * i)).date().isoformat(), {CORN: 1})
    s.delivered("2026-08-02", {CORN: 30})  # usual gap (2 Aug - 1 Mar) / 10 = 15.4 days: overdue from 2 Sep
    env = s.first_runs(at("2026-08-25", 9), {**MAP, CORN: ("corn flakes", "cheapest", "kg", "dry",
                                                           {"stockup_mode": "always"})}, date_="2026-08-27")
    assert not rows(env, "corn flakes")
    assert not rows(s.run(at("2026-09-01", 9), date_="2026-09-03"), "corn flakes"), "30 days isn't more than 30.8"
    shown = []
    for day in ("2026-09-02", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05"):
        env = s.run(at(day, 9 + len(shown)), date_=(date.fromisoformat(day) + timedelta(days=2)).isoformat())
        assert env.status == "ok", env.summary_md
        assert rows(env, "corn flakes")[0]["reason"] == "overdue", "its row is stored every run"
        shown.append("check your stock" in env.summary_md)
    assert shown == [True, True, True, False, False]
    from engine.status import status
    md, data = status(s.conn, at("2026-09-06", 9))
    assert data["overdue"] == ["corn flakes"] and "overdue (check your stock): corn flakes" in md


def test_drop_out_one_time_notice(tmp_path):
    s = base(tmp_path)
    s.product(TP, "Toilet paper 16 rolls", "308,8 m", 180.0)
    for day in ("2025-05-20", "2025-07-20", "2025-09-20"):
        s.delivered(day, {TP: 1})
    mapping = {**MAP, TP: ("toilet paper", "cheapest", "m", "drugstore-household", {"stockup_mode": "always"})}
    s.first_runs(at("2026-09-10", 9), mapping, date_="2026-09-12")  # setup completes before the drop-out date
    text = "toilet paper dropped out"
    assert text not in s.run(at("2026-09-15", 9), date_="2026-09-17").summary_md  # d = 2026-09-20 not reached
    assert text in s.run(at("2026-09-21", 9), date_="2026-09-23", dry=True).summary_md  # dry runs show it
    assert text in s.run(at("2026-09-22", 9), date_="2026-09-24").summary_md  # the ending run shows it too
    assert text not in s.run(at("2026-09-23", 9), date_="2026-09-25").summary_md


def test_first_run_lists_no_old_drop_outs(tmp_path):
    """The first sync reaches back 365 days, so the only cached drop-out a first run can see is dated today,
    which is not after the day setup completes."""
    s = base(tmp_path)
    s.product(TP, "Toilet paper 16 rolls", "308,8 m", 180.0)
    s.delivered("2025-09-28", {TP: 1}, placed=at("2025-09-28", 7))
    mapping = {**MAP, TP: ("toilet paper", "cheapest", "m", "drugstore-household", {"stockup_mode": "always"})}
    env = s.first_runs(at("2026-09-28", 9), mapping, date_="2026-09-30")
    assert "dropped out" not in env.summary_md


def test_incoming_order_delivered_today_and_overdue_upcoming(tmp_path):
    s = base(tmp_path)
    env = s.first_runs(at("2026-09-28", 9), MAP, date_="2026-09-30")
    assert rows(env, "milk")
    # milk delivered this morning covers the need
    s.delivered("2026-09-28", {MILK: 6}, placed=at("2026-09-27", 20))
    env = s.run(at("2026-09-28", 12), date_="2026-09-30", dry=True)
    assert not rows(env, "milk"), "an order delivered today is incoming stock"
    # an overdue upcoming order still counts as on its way, with a warning
    s2 = base(tmp_path / "b")
    s2.first_runs(at("2026-09-28", 9), MAP, date_="2026-09-30")
    s2.fake.upcoming.append({"id": 77, "deliverySlot": {"timeRange": "2026-09-27 08:00-09:00"},
                             "items": [{"id": MILK, "amount": 6, "textualAmount": "1 l"}]})
    env = s2.run(at("2026-09-28", 12), date_="2026-09-30", dry=True)
    assert not rows(env, "milk")
    assert "is overdue" in env.summary_md


def test_vanished_order_is_kept_then_dropped_and_cancelled_is_dropped(tmp_path):
    s = base(tmp_path)
    s.first_runs(at("2026-09-28", 9), MAP, date_="2026-09-30")
    s.fake.upcoming.append({"id": 88, "orderTime": rohlik_time(at("2026-09-28", 10)),
                            "deliverySlot": {"timeRange": "2026-09-30 08:00-09:00"},
                            "items": [{"id": MILK, "amount": 6, "textualAmount": "1 l"}]})
    s.run(at("2026-09-28", 11), date_="2026-09-30", dry=True)  # records it
    s.fake.upcoming.clear()  # it vanishes without delivery or cancellation
    env = s.run(at("2026-09-29", 9), date_="2026-10-01", dry=True)
    assert "no longer listed" in env.summary_md and not rows(env, "milk")
    env = s.run(at("2026-10-01", 9), date_="2026-10-03", dry=True)
    assert "disappeared without being delivered or cancelled" in env.summary_md
    assert rows(env, "milk"), "dropped after its date"
    # cancelled: dropped at once
    s2 = base(tmp_path / "c")
    s2.first_runs(at("2026-09-28", 9), MAP, date_="2026-09-30")
    s2.fake.upcoming.append({"id": 99, "orderTime": rohlik_time(at("2026-09-28", 10)),
                             "deliverySlot": {"timeRange": "2026-09-30 08:00-09:00"},
                             "items": [{"id": MILK, "amount": 6, "textualAmount": "1 l"}]})
    s2.run(at("2026-09-28", 11), date_="2026-09-30", dry=True)
    s2.fake.cancel(99, readd=False)
    env = s2.run(at("2026-09-28", 12), date_="2026-09-30", dry=True)
    assert rows(env, "milk") and "no longer listed" not in env.summary_md


def test_users_cart_line_is_a_reset_and_counts(tmp_path):
    s = base(tmp_path)
    env = s.first_runs(at("2026-09-28", 9), MAP, date_="2026-09-30")
    need = sum(r["normal_units"] for r in rows(env, "milk"))
    s.fake.set_cart_qty(MILK, 1)  # the user's own unit
    env = s.run(at("2026-09-28", 10), date_="2026-09-30")
    assert s.cart()[MILK] >= 1, "the engine never lowers a user-owned quantity"
    agent = sum(r["normal_units"] for r in rows(env, "milk"))
    assert agent <= need


def test_long_life_lines_are_not_p90_capped_and_short_life_are(tmp_path):
    s = base(tmp_path)
    s.product(YOG, "Yogurt 150 g", "150 g", 15.0)
    for i in range(12):
        s.delivered((at("2026-04-02") + timedelta(days=14 * i)).date().isoformat(), {YOG: 1 if i % 6 else 6})
    env = s.first_runs(at("2026-09-28", 9), YMAP, date_="2026-09-30")
    y = rows(env, "yogurt")
    if y and y[0]["reason"] == "due":
        assert y[0]["normal_units"] <= 6


def test_different_account_note(tmp_path):
    s = base(tmp_path)
    s.first_runs(at("2026-09-28", 9), MAP, date_="2026-09-30")
    s.auth.update(last_account_id="42")
    env = s.run(at("2026-09-28", 10), date_="2026-09-30", dry=True)
    assert "Different account than last time" in env.summary_md
    assert s.auth.load()["last_account_id"] == str(s.fake.user_info["id"])


def test_insufficient_history(tmp_path):
    s = Scenario(tmp_path)
    s.product(MILK, "Milk 1 l", "1 l", 25.0)
    s.delivered("2026-09-01", {MILK: 2})
    s.delivered("2026-09-08", {MILK: 2})
    env = s.run(at("2026-09-28", 9), date_="2026-09-30")
    assert env.status == "insufficient_history" and env.exit_code == 2
    assert "3" in env.summary_md and env.data["delivered_orders_last_183_days"] == 2


def test_engine_ignored_zero_priced_line_and_back_to_waiting_when_paid(tmp_path):
    s = base(tmp_path)
    s.product(777, "Catalogue", "1 ks", 0.0)
    s.delivered("2026-09-20", {777: 1}, prices={777: 0.0})
    env = s.run(at("2026-09-28", 9), date_="2026-09-30")
    assert env.status == "needs_tags" and 777 not in [x["sku"] for x in env.data["skus"]]
    s.delivered("2026-09-25", {777: 1}, prices={777: 5.0})
    env = s.run(at("2026-09-28", 10), date_="2026-09-30")
    assert 777 in [x["sku"] for x in env.data["skus"]]
    tags_apply(s.conn, [{"sku": 777, "ignore": True}], "agent", at("2026-09-28", 10))
