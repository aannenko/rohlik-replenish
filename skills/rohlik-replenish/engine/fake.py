"""A fake Rohlik MCP server (spec §9 `--fixtures DIR`, §13.2): serves recorded, anonymised responses and
simulates the cart, so `run` can be driven end to end without the network.

Fixture directory layout (all optional except user_info.json):
  user_info.json      get_user_info payload
  delivered.json      list of delivered orders as fetch_orders returns them
  upcoming.json       list of upcoming orders
  cancelled.json      list of cancelled orders
  cart.json           get_cart `data` object (or just its `items` map)
  products.json       map product ID -> get_product_details entry
  timeslots.json      map "0".."3" -> get_timeslots_checkout payload
  checkout_link.json  get_checkout_link payload
  settings.json       {"failures": {"<tool>[:<order_type>|:<timeslots_day>]": "message"},
                       "transport_failures": ["<tool>[:<order_type>|:<timeslots_day>]", ...]}
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .rohlik import RawResult, TransportError
from .sync import _num
from .timeutil import local_date, parse_rohlik_time

TOOLS = ("fetch_orders", "get_product_details", "get_cart", "get_timeslots_checkout", "get_user_info",
         "get_checkout_link", "add_items_to_cart", "update_cart_item", "remove_cart_item", "clear_cart",
         "cancel_order", "reserve_timeslot")


class FakeRohlik:
    def __init__(self, *, user_info: dict | None = None, delivered: list | None = None, upcoming: list | None = None,
                 cancelled: list | None = None, cart: dict | None = None, products: dict | None = None,
                 timeslots: dict | None = None, checkout_link: Any = None, cart_info: dict | None = None):
        self.user_info = user_info or {"id": 1000001, "xtra": {"limits": {"orders_without_min_remaining": 2}}}
        self.delivered = list(delivered or [])
        self.upcoming = list(upcoming or [])
        self.cancelled = list(cancelled or [])
        self.cart: dict[int, dict] = {int(k): dict(v) for k, v in (cart or {}).items()}
        self.products: dict[int, dict] = {int(k): v for k, v in (products or {}).items()}
        self.timeslots = timeslots or {}
        self.checkout_link = checkout_link if checkout_link is not None else {
            "checkout_link": "https://www.rohlik.cz/pay-by-link?origin=MCP"}
        self.cart_info = cart_info or {"minimalStandardOrderPrice": 749.0, "minimalDeliveryPointOrderPrice": 500,
                                       "minimalOrderPrice": 749.0}
        self.failures: dict[str, str] = {}  # tool, tool:order_type or tool:timeslots_day -> tool error message
        self.transport_failures: set[str] = set()
        self.write_failures: dict[int, str] = {}  # sku -> error message for writes
        self.calls: list[tuple[str, dict]] = []
        self.writes: list[tuple[str, dict]] = []
        self.before_call: Callable[[str, dict], None] | None = None  # test hook (user edits, crashes)
        self.closed = False

    # ------------------------------------------------------------------ loading
    @classmethod
    def from_dir(cls, path: Path) -> "FakeRohlik":
        def load(name, default=None):
            p = path / name
            if not p.exists():
                return default
            return json.loads(p.read_text(encoding="utf-8"))
        cart = load("cart.json", {}) or {}
        cart_info = None
        if "items" in cart:
            cart_info = {k: v for k, v in cart.items() if k not in ("items", "totalPrice", "totalSavings")}
            cart = cart["items"]
        settings = load("settings.json", {}) or {}
        fake = cls(user_info=load("user_info.json"), delivered=load("delivered.json", []),
                   upcoming=load("upcoming.json", []), cancelled=load("cancelled.json", []), cart=cart,
                   products=load("products.json", {}), timeslots=load("timeslots.json", {}),
                   checkout_link=load("checkout_link.json"), cart_info=cart_info)
        fake.failures = dict(settings.get("failures", {}))
        fake.transport_failures = set(settings.get("transport_failures", []))
        return fake

    # ------------------------------------------------------------------ transport protocol
    def list_tools(self) -> list[dict]:
        return [{"name": t} for t in TOOLS]

    def close(self) -> None:
        self.closed = True

    def call(self, tool: str, args: dict, timeout: float) -> RawResult:
        self.calls.append((tool, copy.deepcopy(args)))
        if self.before_call is not None:
            self.before_call(tool, args)
        if tool == "fetch_orders":
            variant = f"{tool}:{args.get('order_type')}"
        elif tool == "get_timeslots_checkout":  # one day's slots can fail on their own (§6.5)
            variant = f"{tool}:{args.get('timeslots_day')}"
        else:
            variant = tool
        if variant in self.transport_failures or tool in self.transport_failures:
            raise TransportError(f"{tool}: connection reset", retryable=False)
        if variant in self.failures or tool in self.failures:
            msg = self.failures.get(variant) or self.failures[tool]
            return RawResult(texts=[msg], is_error=True)
        payload = getattr(self, "_" + tool)(args)
        if isinstance(payload, RawResult):
            return payload
        text = json.dumps(payload, ensure_ascii=False)
        # Rohlik repeats the payload wrapped as {"result": ...} after the first value (§4.4).
        return RawResult(texts=[text + "\n\n" + json.dumps({"result": payload}, ensure_ascii=False)])

    # ------------------------------------------------------------------ reads
    def _get_user_info(self, args):
        return {"success": True, **copy.deepcopy(self.user_info)}  # verified: top-level fields

    def _fetch_orders(self, args):
        if args.get("order_id") is not None:
            pool = [o for o in self.delivered + self.upcoming if int(o["id"]) == int(args["order_id"])]
            return {"success": True, "orders": copy.deepcopy(pool)} if pool else {"success": True, "data": None,
                                                                                   "is_empty": True}
        kind = args.get("order_type")
        limit = min(int(args.get("limit") or 5), 15)
        if kind == "upcoming":
            orders = self.upcoming[:15]
        elif kind == "cancelled":
            orders = self.cancelled[:15]
        elif kind == "delivered":
            def order_day(o):
                t = parse_rohlik_time(o.get("orderTime"))
                return local_date(t) if t else None
            if args.get("date_from") or args.get("date_to"):
                lo, hi = args.get("date_from"), args.get("date_to")
                sel = [o for o in self.delivered
                       if (lo is None or order_day(o).isoformat() >= lo) and (hi is None or order_day(o).isoformat() < hi)]
                orders = sorted(sel, key=lambda o: (o.get("orderTime"), o["id"]))[:15]
            else:
                orders = sorted(self.delivered, key=lambda o: (o.get("orderTime"), o["id"]), reverse=True)[:limit]
        else:
            raise AssertionError(f"fake: order_type {kind!r} not modelled")
        if not orders:
            return {"success": True, "data": None, "is_empty": True}
        return {"success": True, "orders": copy.deepcopy(orders)}

    def _price(self, sku: int) -> float:
        p = (self.products.get(sku) or {}).get("prices") or {}
        sale = _num(p.get("salePrice"))
        return sale if sale is not None else (_num(p.get("originalPrice")) or 0.0)

    def cart_payload(self) -> dict:
        items = {}
        total = 0.0
        for sku, line in sorted(self.cart.items()):
            if (line.get("quantity") or 0) <= 0:
                continue
            items[str(sku)] = copy.deepcopy(line)
            total += self._price(sku) * line["quantity"]
        data = {"cartId": None, "totalPrice": round(total, 2), "totalSavings": 0, "items": items,
                "submitConditionPassed": total >= self.cart_info.get("minimalStandardOrderPrice", 0)}
        data.update(self.cart_info)
        return data

    def _get_cart(self, args):
        return {"status": 200, "messages": [], "data": self.cart_payload(), "success": True}

    def _get_product_details(self, args):
        return {str(i): copy.deepcopy(self.products[int(i)]) for i in args.get("product_ids", [])
                if int(i) in self.products}

    def _get_timeslots_checkout(self, args):
        day = str(args.get("timeslots_day"))
        payload = self.timeslots.get(day)
        if payload is None:
            return {"data": {"availabilityDays": []}}
        return copy.deepcopy(payload)

    def _get_checkout_link(self, args):
        return copy.deepcopy(self.checkout_link)

    # ------------------------------------------------------------------ writes
    def _line(self, sku: int, qty: float) -> dict:
        prod = self.products.get(sku) or {}
        stock = prod.get("stock") or {}
        return {"productId": sku, "productName": prod.get("name", f"#{sku}"), "quantity": qty,
                "textualAmount": prod.get("textualAmount"), "maxBasketAmount": stock.get("maxAvailableAmount", 50),
                "maxBasketAmountReason": None}

    def _write_error(self, sku: int):
        if sku in self.write_failures:
            return RawResult(texts=[self.write_failures[sku]], is_error=True)
        return None

    def _add_items_to_cart(self, args):
        self.writes.append(("add_items_to_cart", copy.deepcopy(args)))
        failed = []
        for item in args.get("items", []):
            sku = int(item["productId"])
            if sku in self.write_failures:  # Rohlik reports success with items_failed_to_add (verified)
                failed.append({"productId": sku, "reason": self.write_failures[sku]})
                continue
            if sku in self.cart:
                self.cart[sku]["quantity"] += item["quantity"]
            else:
                self.cart[sku] = self._line(sku, item["quantity"])
        return {"success": True, "items_failed_to_add": failed,
                "items": [{"productId": int(i["productId"])} for i in args.get("items", [])]}

    def _update_cart_item(self, args):
        self.writes.append(("update_cart_item", copy.deepcopy(args)))
        sku = int(args["product_id"])
        err = self._write_error(sku)
        if err:
            return err
        qty = int(args["quantity"])
        if sku not in self.cart:  # verified (V4): update_cart_item can't create a line
            return RawResult(texts=[f"Cart item not found (product_id={sku})."], is_error=True)
        if qty == 0:
            del self.cart[sku]
        else:
            self.cart[sku]["quantity"] = qty
        return {"success": True}

    def _remove_cart_item(self, args):
        self.writes.append(("remove_cart_item", copy.deepcopy(args)))
        self.cart.pop(int(args["product_id"]), None)
        return {"success": True}

    def _clear_cart(self, args):  # must never be reached: the allowlist refuses it first
        raise AssertionError("clear_cart reached the fake server")

    _cancel_order = _reserve_timeslot = _clear_cart

    # ------------------------------------------------------------------ test helpers
    def checkout(self, order_id: int, order_time: str, slot: str, *, clear_cart: bool = True,
                 with_order_time: bool = False) -> dict:
        """Turn the cart into an upcoming order (as a user checking out in the app)."""
        items = [{"id": sku, "name": line.get("productName"), "amount": line["quantity"],
                  "textualAmount": line.get("textualAmount")} for sku, line in sorted(self.cart.items())
                 if line["quantity"] > 0]
        order = {"id": order_id, "deliverySlot": {"timeRange": slot}, "items": items,
                 "priceComposition": {"total": {"amount": 0.0, "currency": "CZK"}}}
        if with_order_time:
            order["orderTime"] = order_time
        order["_orderTime"] = order_time
        self.upcoming.append(order)
        if clear_cart:
            self.cart = {}
        return order

    def deliver(self, order_id: int, weights: dict[int, float] | None = None, claims: dict[int, int] | None = None
                ) -> dict:
        """Move an upcoming order to the delivered list in Rohlik's delivered format."""
        order = next(o for o in self.upcoming if int(o["id"]) == order_id)
        self.upcoming.remove(order)
        delivered = {"id": order_id, "orderTime": order.get("orderTime") or order.get("_orderTime"),
                     "deliverySlot": order["deliverySlot"], "state": "DELIVERED", "items": [],
                     "priceComposition": {"total": {"amount": 0.0, "currency": "CZK"}},
                     "warrantyInfo": {"enabledData": [], "disabledData": []}}
        total = 0.0
        for it in order["items"]:
            sku = int(it["id"])
            price = self._price(sku) * it["amount"]
            total += price
            delivered["items"].append({"id": sku, "name": it.get("name"), "quantity": it["amount"],
                                       "unit": "kg", "textualAmount": it.get("textualAmount"),
                                       "totalPrice": round(price, 2), "currency": "CZK"})
            weighted = bool((self.products.get(sku) or {}).get("_weighted"))
            delivered["warrantyInfo"]["enabledData"].append({
                "productId": sku, "pieces": it["amount"], "alreadyClaimedPieces": (claims or {}).get(sku, 0),
                "weighted": weighted, "totalWeight": (weights or {}).get(sku)})
        delivered["priceComposition"]["total"]["amount"] = round(total, 2)
        self.delivered.append(delivered)
        return delivered

    def cancel(self, order_id: int, readd: bool = True) -> None:
        order = next(o for o in self.upcoming if int(o["id"]) == order_id)
        self.upcoming.remove(order)
        self.cancelled.append({"id": order_id, "orderTime": order.get("orderTime") or order.get("_orderTime"),
                               "itemsCount": len(order["items"]), "state": "CANCELLED"})
        if readd:
            for it in order["items"]:
                sku = int(it["id"])
                if sku in self.cart:
                    self.cart[sku]["quantity"] += it["amount"]
                else:
                    self.cart[sku] = self._line(sku, it["amount"])

    def set_cart_qty(self, sku: int, qty: float) -> None:
        if qty <= 0:
            self.cart.pop(sku, None)
        elif sku in self.cart:
            self.cart[sku]["quantity"] = qty
        else:
            self.cart[sku] = self._line(sku, qty)
