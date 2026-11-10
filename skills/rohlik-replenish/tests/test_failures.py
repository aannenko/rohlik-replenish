"""§13.1 Missing live data, Cart reads and Envelopes; §13.2 product details failing for one agent-owned SKU or for
every ID, and a later run's "Check these tags" (§8.1)."""

import json
from datetime import timedelta

import pytest

from tests.test_cycles_writes import CHEESE, MILK, YOG, Week, household


def last_run(s):
    return s.conn.execute("SELECT run_id, state FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()


def written(writes) -> set[int]:
    out: set[int] = set()
    for tool, args in writes:
        if tool == "add_items_to_cart":
            out |= {int(i["productId"]) for i in args["items"]}
        else:
            out.add(int(args["product_id"]))
    return out


# ------------------------------------------------------------------------------ missing live data
def test_a_tag_whose_agent_owned_sku_got_no_details_is_kept_as_it_was(tmp_path):
    """§6.11 rule 3: couldn't check isn't the same as no longer wanted. Rohlik now caps milk and yogurt at
    1 per order: re-planned, both agent lines would be cut to 1; the milk, without details today, is kept."""
    s = household(tmp_path)
    w = Week(s, 0)
    w.run()
    first = s.cart()
    assert first.get(MILK, 0) > 1 and first.get(YOG, 0) > 1
    for sku in (MILK, YOG):
        s.fake.cart[sku]["maxBasketAmount"] = 1
    del s.fake.products[MILK]  # Rohlik returns no details for the milk today
    n = len(s.fake.writes)
    env = s.run(w.now + timedelta(minutes=10), date_=w.t)
    assert env.status == "ok", env.summary_md
    assert MILK not in written(s.fake.writes[n:]) and s.cart()[MILK] == first[MILK]
    assert YOG in written(s.fake.writes[n:]) and s.cart()[YOG] == 1, "the other tags are re-planned"
    assert env.data["kept_unchecked"] == ["milk 1.5%"]
    line = next(x for x in env.summary_md.splitlines() if "couldn't check today" in x)
    assert "milk 1.5%" in line and "kept as it was" in line
    assert not [r for r in env.data["plan"] if r["tag"] == "milk 1.5%"], "a kept tag isn't re-planned"
    # with its details back, the milk is re-planned
    s.product(MILK, "Milk 1.5% 1 l", "1 l", 25.0)
    env = s.run(w.now + timedelta(minutes=20), date_=w.t)
    assert env.status == "ok" and not env.data["kept_unchecked"] and s.cart()[MILK] == 1


def test_details_failing_for_every_id_fail_the_run_before_any_write(tmp_path):
    s = household(tmp_path)
    s.fake.failures["get_product_details"] = "upstream error"
    mark = len(s.fake.calls)
    env = Week(s, 0).run()
    assert env.status == "error" and env.data["stage"] == "details" and env.data["wrote"] is False, env.summary_md
    assert env.data["actions"] and not s.fake.writes
    assert last_run(s)["state"] == "failed"
    batches = [sorted(a["product_ids"]) for t, a in s.fake.calls[mark:] if t == "get_product_details"]
    assert batches[0] == [MILK, YOG, CHEESE] and sorted(batches[1:]) == [[MILK], [YOG], [CHEESE]], \
        "after a tool error the batch is retried one ID at a time"


# ------------------------------------------------------------------------------ cart reads
@pytest.mark.parametrize("items", [
    {str(MILK): {"productId": MILK, "productName": "Milk 1.5% 1 l"}},
    {str(MILK): {"productId": MILK, "quantity": None}},
    {str(MILK): {"productId": MILK, "quantity": -1}},
    {str(MILK): {"productId": MILK, "quantity": "two"}},
    [{"productId": MILK, "quantity": 1}],
    None,
], ids=["no-quantity", "null-quantity", "negative", "text", "items-a-list", "no-items"])
def test_an_unreadable_cart_fails_the_read_with_zero_writes(tmp_path, items):
    """§4.4: never read as 0, or the user's units could be overwritten."""
    s = household(tmp_path)
    data = {"cartId": None, "totalPrice": 0, "totalSavings": 0}
    if items is not None:
        data["items"] = items
    s.fake._get_cart = lambda args: {"status": 200, "messages": [], "data": data, "success": True}
    env = Week(s, 0).run()
    assert env.status == "error" and env.data["stage"] == "sync" and env.data["wrote"] is False, env.summary_md
    assert not s.fake.writes and last_run(s)["state"] == "failed"


# ------------------------------------------------------------------------------ envelopes
def test_an_unexpected_exception_gives_one_internal_envelope_and_a_failed_run_with_its_upcoming_orders(
        tmp_path, monkeypatch):
    import engine.runner as runner

    s = household(tmp_path)
    w = Week(s, 0)
    w.run()
    oid = w.checkout_and_deliver()

    def broken(inp, raw_units=False):
        raise ZeroDivisionError("a bug in the planner")
    monkeypatch.setattr(runner, "build_plan", broken)
    n = len(s.fake.writes)
    env = s.run(w.now + timedelta(hours=2), date_=w.t)
    assert env.status == "error" and env.data["stage"] == "internal" and env.data["wrote"] is False
    assert "ZeroDivisionError" in env.summary_md and env.data["actions"]
    assert len(s.fake.writes) == n
    run_id, state = last_run(s)
    assert state == "failed"
    orders = {r[0] for r in s.conn.execute("SELECT order_id FROM run_upcoming_orders WHERE run_id = ?", (run_id,))}
    assert orders == {oid}


def test_every_command_prints_one_envelope_on_an_unexpected_exception(tmp_path, capsys, monkeypatch):
    from engine import cli

    def broken(*args, **kwargs):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(cli, "_offline", broken)
    monkeypatch.setenv("ROHLIK_REPLENISH_DATA_DIR", str(tmp_path))
    code = cli.main(["status"])
    env = json.loads(capsys.readouterr().out)
    assert code == 1 and env["status"] == "error" and env["data"]["stage"] == "internal"
    assert env["data"]["wrote"] is False and env["data"]["actions"]


def test_a_run_failing_at_a_later_read_stores_the_upcoming_orders_it_read(tmp_path):
    """§7.1: they are incoming stock and first sightings."""
    s = household(tmp_path)
    w = Week(s, 0)
    w.run()
    oid = w.checkout_and_deliver()
    s.fake.failures["fetch_orders:delivered"] = "upstream error"
    env = s.run(w.now + timedelta(hours=2), date_=w.t)
    assert env.status == "error" and env.data["stage"] == "sync" and env.data["wrote"] is False
    run_id, state = last_run(s)
    assert state == "failed"
    orders = {r[0] for r in s.conn.execute("SELECT order_id FROM run_upcoming_orders WHERE run_id = ?", (run_id,))}
    assert orders == {oid}


def test_an_unreadable_order_fails_at_the_sync_stage(tmp_path):
    """A sync error is reported at its stage with its own actions, not as `internal`."""
    s = household(tmp_path)
    s.fake.upcoming.append({"id": 4242, "items": [], "priceComposition": {"total": {"amount": 0.0}}})
    env = Week(s, 0).run()
    assert env.status == "error" and env.data["stage"] == "sync" and env.data["wrote"] is False
    assert "4242" in env.summary_md and env.data["actions"]
    assert not s.fake.writes


# ------------------------------------------------------------------------------ check these tags (§8.1, §13.2)
KEFIR, RICE = 104, 105


def test_check_these_tags_lists_only_the_products_the_agent_tagged_since_the_previous_run(tmp_path):
    """§8.1 section 5, §13.2: grouped by storage tag; the user's own corrections are never listed back."""
    s = household(tmp_path)
    w = Week(s, 0)
    assert "Check these tags" not in w.run().summary_md
    s.product(KEFIR, "Kefir 500 ml", "500 ml", 22.0)
    s.product(RICE, "Rice 1 kg", "1 kg", 40.0)
    s.fake.set_cart_qty(KEFIR, 1)  # the user adds two new products himself
    s.fake.set_cart_qty(RICE, 1)
    env = w.run(minutes=10)
    assert env.status == "needs_tags" and {x["sku"] for x in env.data["skus"]} == {KEFIR, RICE}
    s.tag_all({KEFIR: ("kefir", "cheapest", "l", "chilled"), RICE: ("rice", "cheapest", "kg", "dry")},
              w.now + timedelta(minutes=15))
    corrected = s.conn.execute("SELECT sku, storage_tag FROM skus WHERE product_tag_id IS NOT NULL AND sku NOT IN "
                               "(?, ?) ORDER BY sku LIMIT 1", (KEFIR, RICE)).fetchone()
    s.tag([{"sku": corrected[0], "product_tag": "renamed by the user", "policy": "cheapest", "base_unit": "kg",
            "storage_tag": corrected[1], "units_per_pack": 1.0}], w.now + timedelta(minutes=16), source="user")
    env = w.run(minutes=20)
    assert env.status == "ok", env.summary_md
    section = env.summary_md.split("### Check these tags\n")[1].split("\n### ")[0].strip().splitlines()
    assert section == ["- Chilled — kefir (cheapest): Kefir 500 ml", "- Dry — rice (cheapest): Rice 1 kg", "",
                       "Tell me if any is wrong; otherwise they stay."], section
    assert "Check these tags" not in w.run(minutes=30).summary_md, "asked once"
