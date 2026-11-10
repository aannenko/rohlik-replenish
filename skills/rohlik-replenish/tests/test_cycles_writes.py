"""§13.1 miss rule, edits/locks, writes, crash recovery, checkout states, cycles — driven end to end."""

from datetime import timedelta

import pytest

from engine.rohlik import TransportError
from tests.helpers import Scenario, at, d, section

MILK, YOG, CHEESE = 101, 102, 103
MAP = {MILK: ("milk 1.5%", "cheapest", "l", "chilled"), YOG: ("yogurt plain", "cheapest", "kg", "chilled"),
       CHEESE: ("cheese", "cheapest", "kg", "chilled")}


def household(tmp_path, **kw):
    s = Scenario(tmp_path, **kw)
    s.product(MILK, "Milk 1.5% 1 l", "1 l", 25.0)
    s.product(YOG, "Yogurt 150 g", "150 g", 15.0)
    s.product(CHEESE, "Cheese 200 g", "200 g", 60.0)
    s.history("2026-04-01", 7, 26, {MILK: 2, YOG: 3, CHEESE: 1})
    s.first_runs(at("2026-09-28", 9), MAP, date_="2026-09-30")
    return s


class Week:
    """Run, let the user act on the cart, check out, deliver: one cycle per week."""

    def __init__(self, s: Scenario, k: int):
        self.s = s
        self.now = at("2026-09-28", 9) + timedelta(days=7 * k)
        self.t = (self.now + timedelta(days=2)).date().isoformat()

    def run(self, minutes: int = 0, **kw):
        return self.s.run(self.now + timedelta(minutes=minutes), date_=self.t, **kw)

    def checkout_and_deliver(self, minutes: int = 30):
        oid = self.s.checkout(self.now + timedelta(minutes=minutes), self.t)
        # the next run sees it upcoming first (V5: upcoming orders may lack orderTime), then delivered
        return oid


def deliver_all(s: Scenario):
    for o in list(s.fake.upcoming):
        s.deliver(int(o["id"]))


def paused(env) -> list[str]:
    return env.data.get("paused", [])


# ------------------------------------------------------------------------------ misses
def test_two_removals_pause_and_a_purchase_unpauses(tmp_path):
    s = household(tmp_path)
    for k in range(2):
        w = Week(s, k)
        env = w.run()
        assert env.status == "ok", env.summary_md
        assert s.cart().get(MILK), f"week {k}: milk should be added"
        s.fake.set_cart_qty(MILK, 0)  # the user removes the agent's milk
        w.checkout_and_deliver()
        s.run(w.now + timedelta(hours=3), date_=w.t)  # sees the upcoming order
        deliver_all(s)
    w = Week(s, 2)
    env = w.run()
    assert "milk 1.5%" in paused(env)
    assert MILK not in s.cart()
    assert "buy it once" in env.summary_md
    # the user buys it himself: unpaused next time
    s.fake.set_cart_qty(MILK, 1)
    w.checkout_and_deliver()
    s.run(w.now + timedelta(hours=3), date_=w.t)
    deliver_all(s)
    env = Week(s, 3).run()
    assert "milk 1.5%" not in paused(env)


def test_a_paused_tag_is_a_reminder_for_three_finished_runs_then_only_in_status(tmp_path):
    """§2 Reminder: from the day the tag paused, every run shows it (dry runs too) until 3 non-dry runs have
    finished normally; a crashed run, even once recovered, doesn't count; then only `status` lists it."""
    from engine.status import status

    s = household(tmp_path)
    text = "Paused: **milk 1.5%**"
    for k in range(2):
        w = Week(s, k)
        w.run()
        s.fake.set_cart_qty(MILK, 0)  # the user removes the agent's milk
        w.checkout_and_deliver()
        env = s.run(w.now + timedelta(hours=3), date_=w.t)  # sees the order; after week 1's, milk is paused
        deliver_all(s)
    shown = [text in env.summary_md]
    # §8.1 section 4: the line keeps its wording under the "Reminders" heading
    assert ("- Paused: **milk 1.5%** — removed twice; buy it once, or ask me to add it, and I'll resume"
            in section(env.summary_md, "Reminders")) and "### Paused" not in env.summary_md
    w = Week(s, 2)
    shown.append(text in w.run(dry=True).summary_md)
    s.fake.before_call = crash_at_final_reread(s)
    env = w.run(minutes=10)
    s.fake.before_call = None
    assert env.status == "error" and env.data["wrote"] is True, "a crash after a write"
    for minutes in (20, 30, 40):  # the first recovers the crashed run
        shown.append(text in w.run(minutes=minutes).summary_md)
    assert shown == [True, True, True, True, False], "3 finished non-dry runs; dry and crashed runs don't count"
    md, data = status(s.conn, w.now + timedelta(hours=1))
    assert data["paused"] == ["milk 1.5%"] and "paused: milk 1.5%" in md


def test_reduced_quantity_is_not_a_miss(tmp_path):
    s = household(tmp_path)
    for k in range(2):
        w = Week(s, k)
        w.run()
        qty = s.cart()[MILK]
        if qty > 1:
            s.fake.set_cart_qty(MILK, qty - 1)
        w.checkout_and_deliver()
        s.run(w.now + timedelta(hours=3), date_=w.t)
        deliver_all(s)
    env = Week(s, 2).run()
    assert "milk 1.5%" not in paused(env)


def test_failed_write_is_never_a_miss(tmp_path):
    s = household(tmp_path)
    s.fake.write_failures[MILK] = "sold out"
    for k in range(2):
        w = Week(s, k)
        env = w.run()
        assert MILK not in s.cart()
        assert any(x["sku"] == MILK and x["outcome"] == "failed" for x in env.data["writes"])
        w.checkout_and_deliver()
        s.run(w.now + timedelta(hours=3), date_=w.t)
        deliver_all(s)
    s.fake.write_failures.clear()
    env = Week(s, 2).run()
    assert "milk 1.5%" not in paused(env)
    assert s.cart().get(MILK)


def test_retry_after_an_unreadable_cart_never_adds_on_top(tmp_path):
    # V4: add_items_to_cart adds to an existing line. When a write fails and the re-read fails too, the line's
    # state is unknown, so the one retry must be the idempotent update_cart_item (§6.15 step 4).
    s = household(tmp_path)
    s.fake.set_cart_qty(MILK, 1)  # the user's own milk: the agent's units go on top, by update
    failed = {"update": False, "reread": False}

    def flaky(tool, args):
        if tool == "update_cart_item" and int(args["product_id"]) == MILK and not failed["update"]:
            failed["update"] = True
            raise TransportError("timeout", retryable=False)
        if tool == "get_cart" and failed["update"] and not failed["reread"]:
            failed["reread"] = True
            raise TransportError("connection reset", retryable=False)
    s.fake.before_call = flaky
    env = Week(s, 0).run()
    s.fake.before_call = None
    assert failed == {"update": True, "reread": True}, "the scenario must reach the retry"
    target = s.rows(env)[MILK]["target"]
    assert target > 1
    assert not [a for t, a in s.fake.writes if t == "add_items_to_cart"
                and any(int(i["productId"]) == MILK for i in a["items"])]
    assert s.cart()[MILK] == target


def test_retry_failing_for_the_product_continues_after_a_transport_error(tmp_path):
    # §6.15 step 4: the retry's error decides. The transport recovered and the product is sold out: write the rest.
    s = household(tmp_path)
    tripped = {"done": False}

    def flaky(tool, args):
        if (tool == "add_items_to_cart" and not tripped["done"]
                and any(int(i["productId"]) == MILK for i in args["items"])):
            tripped["done"] = True
            s.fake.write_failures[MILK] = "sold out"
            raise TransportError("timeout", retryable=False)
    s.fake.before_call = flaky
    env = Week(s, 0).run()
    s.fake.before_call = None
    assert tripped["done"]
    outcomes = {w["sku"]: w["outcome"] for w in env.data["writes"]}
    assert outcomes[MILK] == "failed"
    assert outcomes.get(YOG) == "confirmed" and s.cart().get(YOG), "yogurt comes after milk and is still written"


def test_manual_order_creates_no_miss_but_clears(tmp_path):
    s = household(tmp_path)
    w = Week(s, 0)
    w.run()
    s.fake.set_cart_qty(MILK, 0)
    w.checkout_and_deliver()
    s.run(w.now + timedelta(hours=3), date_=w.t)
    deliver_all(s)  # one miss
    # a manual order (no run in its cycle) with milk clears the miss
    s.fake.set_cart_qty(MILK, 1)
    s.checkout(w.now + timedelta(days=3), (w.now + timedelta(days=4)).date().isoformat())
    deliver_all(s)
    w1 = Week(s, 1)
    w1.run()
    s.fake.set_cart_qty(MILK, 0)
    w1.checkout_and_deliver()
    s.run(w1.now + timedelta(hours=3), date_=w1.t)
    deliver_all(s)
    env = Week(s, 2).run()
    assert "milk 1.5%" not in paused(env), "the miss before the manual order was cleared"


# ------------------------------------------------------------------------------ edits and locks
def test_user_edit_locks_the_tag_for_the_cycle_and_reruns_leave_it(tmp_path):
    s = household(tmp_path)
    w = Week(s, 0)
    w.run()
    s.fake.set_cart_qty(MILK, 5)  # the user changes the agent's line
    env = w.run(minutes=10)
    assert "milk 1.5%" in env.data["locked"]
    assert s.cart()[MILK] == 5
    env = w.run(minutes=20)
    assert "milk 1.5%" in env.data["locked"] and s.cart()[MILK] == 5
    assert "milk" not in env.summary_md.lower(), "§8.1: a locked tag isn't listed; the user set its line himself"


def test_removed_line_stays_removed_across_reruns(tmp_path):
    s = household(tmp_path)
    w = Week(s, 0)
    w.run()
    s.fake.set_cart_qty(YOG, 0)
    for m in (10, 20):
        w.run(minutes=m)
        assert YOG not in s.cart()


def test_three_runs_with_unchanged_inputs_make_zero_writes(tmp_path):
    s = household(tmp_path)
    w = Week(s, 0)
    w.run()
    n = len(s.fake.writes)
    cart = s.cart()
    for m in (5, 10, 15):
        env = w.run(minutes=m)
        assert env.data["writes"] == []
    assert len(s.fake.writes) == n and s.cart() == cart


def test_change_just_before_writing_skips_the_whole_tag_and_locks_it(tmp_path):
    s = household(tmp_path)
    w = Week(s, 0)
    start = len(s.fake.calls)

    def edit(tool, args):
        # the user adds cheese in the app between the planning read and the pre-write re-read
        reads = [c for c in s.fake.calls[start:] if c[0] == "get_cart"]
        if tool == "get_cart" and len(reads) == 2:
            s.fake.set_cart_qty(CHEESE, 2)
    s.fake.before_call = edit
    env = w.run()
    s.fake.before_call = None
    assert "cheese" in env.data["skipped_tags"]
    assert s.cart()[CHEESE] == 2, "the user's units stay the user's"
    assert s.cart().get(MILK), "other tags are written"
    env = w.run(minutes=10)
    assert "cheese" in env.data["locked"]
    assert s.cart()[CHEESE] == 2


def test_lines_written_then_changed_before_the_final_reread_are_edits_next_run(tmp_path):
    s = household(tmp_path)
    w = Week(s, 0)
    calls = {"n": 0}

    def edit(tool, args):
        if tool in ("update_cart_item", "add_items_to_cart"):
            calls["n"] += 1
        if tool == "get_cart" and calls["n"] >= 3:  # after the writes, before the final re-read
            s.fake.set_cart_qty(MILK, 0)
            s.fake.before_call = None
    s.fake.before_call = edit
    w.run()
    env = w.run(minutes=10)
    assert "milk 1.5%" in env.data["locked"]
    assert MILK not in s.cart()


# ------------------------------------------------------------------------------ crash recovery
class Crash(BaseException):
    """The process dies: nothing in the engine catches this."""


def test_crash_between_two_writes_is_recovered(tmp_path):
    s = household(tmp_path)
    w = Week(s, 0)
    seen = {"writes": 0}

    def crash(tool, args):
        if tool in ("update_cart_item", "add_items_to_cart"):
            seen["writes"] += 1
            if seen["writes"] == 2:
                raise Crash()
    s.fake.before_call = crash
    with pytest.raises(Crash):
        w.run()
    s.fake.before_call = None
    state = s.conn.execute("SELECT state FROM runs ORDER BY started_at DESC, run_id DESC LIMIT 1").fetchone()[0]
    assert state == "writing", "a failure after the write-ahead commit leaves the run writing"
    confirmed = s.conn.execute("SELECT COUNT(*) FROM run_cart WHERE after_qty IS NOT NULL AND run_id = "
                               "(SELECT MAX(run_id) FROM runs)").fetchone()[0]
    assert confirmed == 1, "the confirmed first write was stored at once"
    env = w.run(minutes=10)
    assert env.status == "ok", env.summary_md
    row = s.conn.execute("SELECT state, recovered, finished_at FROM runs WHERE state = 'done' AND recovered = 1"
                         ).fetchone()
    assert row is not None and row["finished_at"] is None
    assert s.cart().get(MILK) and s.cart().get(YOG)
    n = len(s.fake.writes)
    w.run(minutes=20)
    assert len(s.fake.writes) == n, "after recovery, re-runs are idempotent"


def test_failure_before_the_write_ahead_commit_is_failed(tmp_path):
    s = household(tmp_path)
    s.fake.failures["fetch_orders:upcoming"] = "upstream error"
    env = Week(s, 0).run()
    assert env.status == "error" and env.data == {"stage": "sync", "wrote": False, "actions": env.data["actions"]}
    assert env.data["actions"]
    state = s.conn.execute("SELECT state FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()[0]
    assert state == "failed"
    assert not s.fake.writes


def test_without_a_refresh_token_the_lifetime_check_uses_the_time_the_writes_start(tmp_path):
    """§7.1 step 11: the access token has 90 s left when the run starts but 30 s when its writes would
    start, so the run asks for a login and writes nothing; with 70 s left at that point it writes."""
    from engine.runner import SessionInfo

    s = household(tmp_path)
    w = Week(s, 0)
    s.session = SessionInfo(has_refresh=False, access_expires_at=w.now.timestamp() + 90)
    env = w.run(clock=lambda: w.now + timedelta(seconds=60))
    assert env.status == "auth_required" and env.data["wrote"] is False, env.summary_md
    assert not s.fake.writes
    state = s.conn.execute("SELECT state FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()[0]
    assert state == "failed"
    later = w.now + timedelta(minutes=5)
    s.session = SessionInfo(has_refresh=False, access_expires_at=later.timestamp() + 90)
    env = w.run(minutes=5, clock=lambda: later + timedelta(seconds=20))
    assert env.status == "ok" and s.fake.writes, env.summary_md


def crash_at_final_reread(s):
    """A failure after a write: the final cart re-read (§6.15 step 5) fails, so the run stays `writing`."""
    n0 = len(s.fake.writes)

    def crash(tool, args):
        if tool == "get_cart" and len(s.fake.writes) > n0:
            raise TransportError("boom", retryable=False)
    return crash


def test_confirmed_write_then_crash_then_user_removal_counts_as_added(tmp_path):
    s = household(tmp_path)
    w = Week(s, 0)
    s.fake.before_call = crash_at_final_reread(s)
    env = w.run()
    s.fake.before_call = None
    assert env.status == "error" and env.data["stage"] == "write" and env.data["wrote"] is True
    s.fake.set_cart_qty(MILK, 0)  # the user removes it while the run is crashed
    env = w.run(minutes=10)
    assert "milk 1.5%" in env.data["locked"], "the removal of a confirmed write is detected as an edit"


def test_failed_checkout_link_is_a_warning_and_the_run_finishes(tmp_path):
    """A failed link read is only a warning; `wrote` means a write was attempted."""
    s = household(tmp_path)
    w = Week(s, 0)
    w.run()
    n = len(s.fake.writes)
    s.fake.failures["get_checkout_link"] = "temporarily unavailable"
    env = w.run(minutes=10)  # unchanged inputs: zero writes
    assert env.status == "ok" and len(s.fake.writes) == n
    assert "checkout link couldn't be read" in env.summary_md
    state, finished = s.conn.execute("SELECT state, finished_at FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    assert state == "done" and finished is not None
    s.fake.failures["fetch_orders:upcoming"] = "upstream error"
    env = w.run(minutes=20)
    assert env.status == "error" and env.data["wrote"] is False, "no write was attempted"


# ------------------------------------------------------------------------------ checkout states
def test_checkout_pending_makes_the_run_dry_until_the_order_appears(tmp_path):
    s = household(tmp_path)
    w = Week(s, 0)
    w.run()
    # the user checked out: the cart is empty but Rohlik hasn't published the order yet
    order = s.fake.checkout(9999, "", f"{w.t} 08:00-09:00")
    s.fake.upcoming.remove(order)
    env = w.run(minutes=30)
    assert env.data["checkout"] == "pending" and env.status == "dry_run"
    assert env.summary_md.strip() == "**Looks like you just checked out — re-run once the order shows up.**"
    assert env.data["plan"] == [], "§6.6: it stops after its reads and plans nothing"
    assert not s.cart()
    kinds = {t for t, _ in s.fake.calls[-6:]}
    assert "get_product_details" not in kinds and "get_timeslots_checkout" not in kinds
    # 24 hours later the rule lapses and the missing lines are the user's edits
    env = s.run(w.now + timedelta(hours=25), date_=w.t)
    assert env.data["checkout"] is None
    assert set(env.data["locked"]) >= {"milk 1.5%"}


def test_checkout_in_progress(tmp_path):
    s = household(tmp_path)
    w = Week(s, 0)
    w.run()
    s.checkout(w.now + timedelta(minutes=20), w.t, clear_cart=False)
    n = len(s.fake.writes)
    env = w.run(minutes=25)
    assert env.data["checkout"] == "in_progress" and env.status == "dry_run"
    assert len(s.fake.writes) == n


def test_checkout_in_progress_only_for_an_order_this_run_sighted_first(tmp_path):
    """Once a run has recorded the order, a re-run whose cart holds the same units runs normally."""
    s = household(tmp_path)
    w = Week(s, 0)
    w.run()
    first = s.cart()
    s.checkout(w.now + timedelta(minutes=20), w.t)
    next_t = (d(w.t) + timedelta(days=7)).isoformat()
    env = s.run(w.now + timedelta(hours=1), date_=next_t)
    assert env.status == "ok" and env.data["checkout"] is None, env.summary_md  # sees the order, refills the cart
    assert all(s.cart().get(sku, 0) >= q for sku, q in first.items()), "the cart holds the first run's units again"
    env = s.run(w.now + timedelta(hours=2), date_=next_t)
    assert env.status == "ok" and env.data["checkout"] is None, "the order was first sighted by an earlier run"


def test_checkout_published_late_does_not_carry_a_lock_into_the_next_cycle(tmp_path):
    s = household(tmp_path)
    w = Week(s, 0)
    w.run()
    order = s.fake.checkout(7777, "", f"{w.t} 08:00-09:00")
    s.fake.upcoming.remove(order)
    env = s.run(w.now + timedelta(hours=26), date_=w.t)  # pending lapsed: the lines look like edits
    assert "milk 1.5%" in env.data["locked"]
    s.fake.upcoming.append(order)  # now Rohlik publishes it
    env = s.run(w.now + timedelta(hours=27), date_=w.t)
    assert env.data["locked"] == [], "the order closed the cycle of the run that proposed those lines"


def test_cancelled_order_items_readded_to_cart_are_the_users(tmp_path):
    s = household(tmp_path)
    w = Week(s, 0)
    w.run()
    oid = w.checkout_and_deliver()
    s.run(w.now + timedelta(hours=2), date_=w.t)  # sees it upcoming
    s.fake.cancel(oid)  # cancelling re-adds the items to the cart
    before = s.cart()
    env = s.run(w.now + timedelta(hours=3), date_=w.t)
    assert env.status == "ok", env.summary_md
    for sku, q in before.items():
        assert s.cart().get(sku, 0) >= q, "the engine never lowers a user-owned quantity"


def test_crashed_run_matched_to_order_without_order_time(tmp_path):
    """§13.2: a crashed run that wrote the cart, followed by a checkout whose upcoming order has no orderTime: the
    order is matched to the crashed run."""
    s = household(tmp_path)
    w = Week(s, 0)
    s.fake.before_call = crash_at_final_reread(s)
    w.run()
    s.fake.before_call = None
    oid = s.checkout(w.now + timedelta(minutes=40), w.t)
    env = s.run(w.now + timedelta(hours=2), date_=w.t)
    assert env.status == "ok"
    from engine.cycles import build_placed, resolve_cycles
    from engine.world import RunRecord, load_world
    world = load_world(s.conn)
    cur = RunRecord("r999999-x", 999999, w.now + timedelta(hours=3), None, "current", False, None, None, {})
    state = resolve_cycles(world, build_placed(world, [], cur, d(w.t)))
    p = state.by_order[oid]
    assert p.cycle is not None and p.matched.recovered, "matched to the crashed (recovered) run"
    assert p.ref_run is p.matched
