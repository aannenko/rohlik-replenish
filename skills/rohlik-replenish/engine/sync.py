"""Sync and coverage (spec §6.1): delivered orders, upcoming orders, cart, vanished/cancelled orders."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .constants import FETCH_LIMIT, FIRST_SYNC_DAYS
from .db import transaction
from .rohlik import RohlikClient
from .timeutil import iso, local_date, local_midnight, parse_rohlik_time, parse_slot


class SyncError(Exception):
    pass


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        return _num(value.get("amount"))
    if isinstance(value, str):
        text = value.replace("\u00a0", " ").strip().split(" ")[0].replace(",", ".")
        try:
            return float(text)
        except ValueError:
            return None
    return None


@dataclass
class OrderLine:
    sku: int
    name: str | None
    quantity: float | None
    weight_kg: float | None
    textual_amount: str | None
    price: float
    claimed: float
    weighted: bool


@dataclass
class DeliveredOrder:
    order_id: int
    order_time: datetime
    delivery_date: date
    slot_end: datetime | None
    total_czk: float | None
    lines: dict[int, OrderLine]


@dataclass
class UpcomingOrder:
    order_id: int
    order_time: datetime | None
    delivery_date: date
    slot_end: datetime | None
    amounts: dict[int, float]
    names: dict[int, str]
    texts: dict[int, str]


@dataclass
class CartLine:
    sku: int
    quantity: float
    textual_amount: str | None
    max_basket: float | None
    max_basket_reason: str | None
    name: str | None


@dataclass
class Cart:
    lines: dict[int, CartLine]
    total: float | None
    minimal_order: float | None
    raw_keys: tuple[str, ...] = ()

    def qty(self, sku: int) -> float:
        line = self.lines.get(sku)
        return line.quantity if line else 0.0

    def quantities(self) -> dict[int, float]:
        return {sku: line.quantity for sku, line in self.lines.items() if line.quantity > 0}


def parse_delivered(o: dict) -> DeliveredOrder:
    order_id = int(o["id"])
    slot = parse_slot((o.get("deliverySlot") or {}).get("timeRange"))
    order_time = parse_rohlik_time(o.get("orderTime"))
    if slot is None:
        if order_time is None:
            raise SyncError(f"order {order_id} has neither a delivery slot nor an order time")
        slot = (local_date(order_time), None)
    delivery_date, slot_end = slot
    if order_time is None:
        order_time = local_midnight(delivery_date)
    total = _num((o.get("priceComposition") or {}).get("total"))
    if total is None:
        total = _num(o.get("totalPrice"))

    warranty: dict[int, dict] = {}
    winfo = o.get("warrantyInfo") or {}
    for key in ("enabledData", "disabledData"):
        for w in winfo.get(key) or []:
            if not isinstance(w, dict) or w.get("productId") is None:
                continue
            pid = int(w["productId"])
            agg = warranty.setdefault(pid, {"pieces": None, "weight": None, "claimed": 0.0, "weighted": False,
                                            "total": 0.0, "name": w.get("name")})
            pieces = _num(w.get("pieces"))
            if pieces is not None:
                agg["pieces"] = (agg["pieces"] or 0.0) + pieces
            weight = _num(w.get("totalWeight"))
            if weight is not None:
                agg["weight"] = (agg["weight"] or 0.0) + weight
            agg["claimed"] += _num(w.get("alreadyClaimedPieces")) or 0.0
            agg["weighted"] = agg["weighted"] or bool(w.get("weighted"))
            agg["total"] += _num(w.get("total")) or 0.0

    items: dict[int, dict] = {}
    for it in o.get("items") or []:
        if not isinstance(it, dict) or it.get("id") is None:
            continue
        pid = int(it["id"])
        agg = items.setdefault(pid, {"quantity": None, "price": 0.0, "text": None, "name": it.get("name")})
        q = _num(it.get("quantity"))
        if q is not None:  # duplicate lines (e.g. a zero-price compensation line) add up
            agg["quantity"] = (agg["quantity"] or 0.0) + q
        agg["price"] += _num(it.get("totalPrice")) or 0.0
        if not agg["text"] and it.get("textualAmount"):
            agg["text"] = str(it["textualAmount"])

    lines: dict[int, OrderLine] = {}
    for pid in sorted(set(items) | set(warranty)):
        it = items.get(pid, {})
        w = warranty.get(pid, {})
        quantity = it.get("quantity")
        if quantity is None:
            quantity = w.get("pieces")
        price = it["price"] if pid in items else (w.get("total") or 0.0)
        lines[pid] = OrderLine(
            sku=pid, name=it.get("name") or w.get("name"), quantity=quantity, weight_kg=w.get("weight"),
            textual_amount=it.get("text"), price=round(price, 4), claimed=w.get("claimed", 0.0) or 0.0,
            weighted=bool(w.get("weighted")))
    return DeliveredOrder(order_id, order_time, delivery_date, slot_end, total, lines)


def parse_upcoming(o: dict) -> UpcomingOrder:
    order_id = int(o["id"])
    order_time = parse_rohlik_time(o.get("orderTime"))
    slot = parse_slot((o.get("deliverySlot") or {}).get("timeRange"))
    if slot is None:
        if order_time is None:
            raise SyncError(f"upcoming order {order_id} has no delivery slot")
        slot = (local_date(order_time), None)
    amounts: dict[int, float] = {}
    names: dict[int, str] = {}
    texts: dict[int, str] = {}
    for it in o.get("items") or []:
        if not isinstance(it, dict):
            continue
        pid = it.get("id", it.get("productId"))
        if pid is None:
            continue
        pid = int(pid)
        amount = _num(it.get("amount"))
        if amount is None:
            amount = _num(it.get("quantity"))
        amounts[pid] = amounts.get(pid, 0.0) + (amount or 0.0)
        if it.get("name"):
            names[pid] = str(it["name"])
        if it.get("textualAmount"):
            texts[pid] = str(it["textualAmount"])
    return UpcomingOrder(order_id, order_time, slot[0], slot[1], amounts, names, texts)


def parse_cart(data: dict) -> Cart:
    """The client has already validated the lines (§4.4); an unreadable quantity is never taken as 0."""
    lines: dict[int, CartLine] = {}
    for key, line in (data.get("items") or {}).items():
        qty = _num(line.get("quantity")) if isinstance(line, dict) else None
        if qty is None or qty < 0:
            raise ValueError(f"cart line {key} has no readable quantity")
        pid = int(line.get("productId", key))
        lines[pid] = CartLine(pid, qty, line.get("textualAmount"), _num(line.get("maxBasketAmount")),
                              line.get("maxBasketAmountReason"),
                              line.get("productName") or line.get("name"))
    return Cart(lines, _num(data.get("totalPrice")), _num(data.get("minimalStandardOrderPrice")),
                tuple(sorted(data.keys())))


@dataclass
class SyncResult:
    new_orders: int = 0
    upcoming: list[UpcomingOrder] = field(default_factory=list)
    cart: Cart | None = None
    vanished_kept: list[tuple[int, date]] = field(default_factory=list)
    vanished_dropped: list[int] = field(default_factory=list)
    cancelled_new: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _order_date(o: dict) -> date | None:
    t = parse_rohlik_time(o.get("orderTime"))
    return local_date(t) if t else None


def _start_of(o: dict) -> date:
    """The earlier of an order's order date and delivery date (V7 is unverified on which ranges filter)."""
    candidates = []
    od = _order_date(o)
    if od:
        candidates.append(od)
    slot = parse_slot((o.get("deliverySlot") or {}).get("timeRange"))
    if slot:
        candidates.append(slot[0])
    if not candidates:
        raise SyncError(f"order {o.get('id')} has no dates")
    return min(candidates)


def read_delivered(conn: sqlite3.Connection, client: RohlikClient, e: date) -> list[dict]:
    newest = client.fetch_orders("delivered")
    cached = {r[0] for r in conn.execute("SELECT order_id FROM orders ORDER BY order_id")}
    raw: dict[int, dict] = {}
    for o in newest:
        raw[int(o["id"])] = o
    newest_ids = set(raw)
    if newest and not (newest_ids & cached):
        row = conn.execute("SELECT order_time, delivery_date FROM orders "
                           "ORDER BY delivery_date DESC, order_time DESC, order_id DESC LIMIT 1").fetchone()
        if row:
            from .timeutil import parse_iso
            start = min(local_date(parse_iso(row["order_time"])), date.fromisoformat(row["delivery_date"]))
        else:
            start = e - timedelta(days=FIRST_SYNC_DAYS)
        end = e + timedelta(days=1)  # date_to is exclusive
        while True:
            page = client.fetch_orders("delivered", date_from=start.isoformat(), date_to=end.isoformat())
            for o in page:
                raw[int(o["id"])] = o
            if len(page) < FETCH_LIMIT or {int(o["id"]) for o in page} & newest_ids:
                break
            nxt = _start_of(page[-1])
            if nxt <= start:
                raise SyncError(f"order history paging does not advance past {start.isoformat()} "
                                f"(15 or more orders on one day)")
            start = nxt
    return [raw[k] for k in sorted(raw)]


def _upsert_sku(conn, sku: int, name: str | None, text: str | None, weighted: bool | None, now: datetime) -> None:
    row = conn.execute("SELECT name, textual_amount, weighted FROM skus WHERE sku = ?", (sku,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO skus(sku, name, textual_amount, weighted, updated_at) VALUES (?,?,?,?,?)",
                     (sku, name or f"#{sku}", text, 1 if weighted else 0, iso(now)))
        return
    new_name = name if name else row["name"]
    new_text = text if text else row["textual_amount"]
    new_weighted = 1 if (weighted or row["weighted"]) else 0
    if (new_name, new_text, new_weighted) != (row["name"], row["textual_amount"], row["weighted"]):
        conn.execute("UPDATE skus SET name = ?, textual_amount = ?, weighted = ? WHERE sku = ?",
                     (new_name, new_text, new_weighted, sku))


def store_delivered(conn: sqlite3.Connection, orders: list[DeliveredOrder], now: datetime) -> int:
    """Upsert inside the caller's transaction. Returns the number of new orders."""
    existing = {r[0] for r in conn.execute("SELECT order_id FROM orders ORDER BY order_id")}
    new = 0
    for o in sorted(orders, key=lambda x: (x.delivery_date, x.order_time, x.order_id)):
        for line in o.lines.values():
            _upsert_sku(conn, line.sku, line.name, line.textual_amount, line.weighted, now)
        if o.order_id in existing:
            conn.execute("UPDATE orders SET order_time=?, delivery_date=?, slot_end=?, total_czk=?, fetched_at=? "
                         "WHERE order_id=?", (iso(o.order_time), o.delivery_date.isoformat(),
                                              iso(o.slot_end) if o.slot_end else None, o.total_czk, iso(now),
                                              o.order_id))
            conn.execute("DELETE FROM order_lines WHERE order_id = ?", (o.order_id,))
        else:
            new += 1
            conn.execute("INSERT INTO orders(order_id, order_time, delivery_date, slot_end, total_czk, fetched_at) "
                         "VALUES (?,?,?,?,?,?)", (o.order_id, iso(o.order_time), o.delivery_date.isoformat(),
                                                  iso(o.slot_end) if o.slot_end else None, o.total_czk, iso(now)))
        conn.executemany(
            "INSERT INTO order_lines(order_id, sku, quantity, weight_kg, textual_amount, line_price_czk, "
            "claimed_quantity) VALUES (?,?,?,?,?,?,?)",
            [(o.order_id, ln.sku, ln.quantity, ln.weight_kg, ln.textual_amount, ln.price, ln.claimed)
             for ln in sorted(o.lines.values(), key=lambda x: x.sku)])
    return new


def reevaluate_auto_ignore(conn: sqlite3.Connection, now: datetime) -> None:
    """SKUs whose cached lines are all zero-priced are ignored by the engine unless assigned (§5.3)."""
    rows = conn.execute(
        "SELECT s.sku, s.assigned_by, COUNT(l.sku) AS n, SUM(CASE WHEN l.line_price_czk > 0 THEN 1 ELSE 0 END) AS paid "
        "FROM skus s LEFT JOIN order_lines l ON l.sku = s.sku "
        "WHERE s.assigned_by IS NULL OR s.assigned_by = 'engine' GROUP BY s.sku ORDER BY s.sku").fetchall()
    for r in rows:
        if r["assigned_by"] is None and r["n"] > 0 and not r["paid"]:
            conn.execute("UPDATE skus SET ignored = 1, assigned_by = 'engine', updated_at = ? WHERE sku = ?",
                         (iso(now), r["sku"]))
        elif r["assigned_by"] == "engine" and r["paid"]:
            conn.execute("UPDATE skus SET ignored = 0, assigned_by = NULL, updated_at = ? WHERE sku = ?",
                         (iso(now), r["sku"]))


def store_upcoming_skus(conn: sqlite3.Connection, upcoming: list[UpcomingOrder], now: datetime) -> None:
    """SKU rows for upcoming orders, inside the caller's transaction (also for a failed run's observations)."""
    for u in sorted(upcoming, key=lambda x: x.order_id):
        for sku in sorted(u.amounts):
            _upsert_sku(conn, sku, u.names.get(sku), u.texts.get(sku), None, now)


def sync_all(conn: sqlite3.Connection, client: RohlikClient, now: datetime, e: date,
             observed: list | None = None) -> SyncResult:
    """§7.1 step 4: read cart, upcoming and delivered orders, resolve vanished orders; one transaction.

    `observed` receives the upcoming orders as soon as they are read, so a run that fails at a later read of this
    step still stores them: they are incoming stock and first sightings."""
    result = SyncResult()
    cart = parse_cart(client.get_cart())
    upcoming = [parse_upcoming(o) for o in client.fetch_orders("upcoming")]
    if observed is not None:
        observed.extend(upcoming)
    delivered = [parse_delivered(o) for o in read_delivered(conn, client, e)]
    delivered_ids = {o.order_id for o in delivered} | {
        r[0] for r in conn.execute("SELECT order_id FROM orders ORDER BY order_id")}
    upcoming_ids = {o.order_id for o in upcoming}
    cancelled = {r[0] for r in conn.execute("SELECT order_id FROM cancelled_orders ORDER BY order_id")}
    recorded = conn.execute(
        "SELECT ru.order_id, MAX(ru.delivery_date) AS d FROM run_upcoming_orders ru "
        "GROUP BY ru.order_id ORDER BY ru.order_id").fetchall()
    vanished = {r["order_id"]: date.fromisoformat(r["d"]) for r in recorded
                if r["order_id"] not in upcoming_ids and r["order_id"] not in delivered_ids
                and r["order_id"] not in cancelled}
    newly_cancelled: list[int] = []
    if vanished:
        listed = {int(o["id"]) for o in client.fetch_orders("cancelled") if o.get("id") is not None}
        newly_cancelled = sorted(set(vanished) & listed)
    with transaction(conn):
        result.new_orders = store_delivered(conn, delivered, now)
        store_upcoming_skus(conn, upcoming, now)
        for sku in sorted(cart.lines):
            line = cart.lines[sku]
            _upsert_sku(conn, sku, line.name, line.textual_amount, None, now)
        for oid in newly_cancelled:
            conn.execute("INSERT OR IGNORE INTO cancelled_orders(order_id, seen_at) VALUES (?, ?)", (oid, iso(now)))
        reevaluate_auto_ignore(conn, now)
    # Last-seen delivery date per vanished order comes from the latest recording run.
    for oid in sorted(set(vanished) - set(newly_cancelled)):
        last = conn.execute(
            "SELECT ru.delivery_date FROM run_upcoming_orders ru JOIN runs r ON r.run_id = ru.run_id "
            "WHERE ru.order_id = ? ORDER BY r.started_at DESC, r.run_id DESC LIMIT 1", (oid,)).fetchone()
        d = date.fromisoformat(last[0])
        if d >= e:
            result.vanished_kept.append((oid, d))
            result.warnings.append(f"order {oid} is no longer listed")
        else:
            result.vanished_dropped.append(oid)
            result.warnings.append(f"order {oid} disappeared without being delivered or cancelled")
    result.upcoming = upcoming
    result.cart = cart
    result.cancelled_new = newly_cancelled
    return result


def update_from_details(conn: sqlite3.Connection, details: dict[int, dict], now: datetime) -> None:
    """Latest texts and names seen in product details (§6.1)."""
    if not details:
        return
    with transaction(conn):
        for sku in sorted(details):
            d = details[sku]
            row = conn.execute("SELECT 1 FROM skus WHERE sku = ?", (sku,)).fetchone()
            if row is None:
                continue
            _upsert_sku(conn, sku, d.get("name"), d.get("textualAmount"), None, now)
