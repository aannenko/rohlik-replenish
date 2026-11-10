"""Backtest (§13.4): replay the cached history, or a CSV dataset loaded into an in-memory database.

Every order delivered at least 183 days after the first one is evaluated at e = T = its delivery date, using
the orders strictly before it, at tag level only. P = the tags that would be added: due, n >= 3, not paused.
"""

from __future__ import annotations

import csv
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .constants import GATE_ADD, GATE_SUGGEST, MIN_HISTORY_DAYS, MISSES_TO_PAUSE
from .cycles import Cycle, CycleState, Placed
from .db import connect, transaction
from .planner import build_events, build_tag_data, cadence_of
from .sizes import parse
from .stock import day_of
from .sync import DeliveredOrder, OrderLine, reevaluate_auto_ignore, store_delivered
from .tagsvc import normalise_name, tags_apply
from .timeutil import local_midnight
from .world import World, load_world

STORAGE_MAP = {"chilled-dairy": "chilled", "chilled-other": "chilled", "dry-canned": "dry"}
_UNIT_RANK = {"kg": 0, "l": 1, "pcs": 2, "m": 3}


@dataclass
class BacktestResult:
    evaluated: int = 0
    proposed_per_order: float = 0.0
    precision_same: float | None = None
    precision_next: float | None = None
    recall_lines: float = 0.0
    recall_spend: float = 0.0
    suggestions_per_order: float = 0.0
    per_order: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        def pct(x):
            return None if x is None else round(x, 4)
        return {"evaluated_orders": self.evaluated, "proposed_tags_per_order": round(self.proposed_per_order, 2),
                "precision_same_order": pct(self.precision_same),
                "precision_this_or_next_order": pct(self.precision_next),
                "recall_lines": pct(self.recall_lines), "recall_spend": pct(self.recall_spend),
                "suggestions_per_order": round(self.suggestions_per_order, 2), "notes": self.notes}


def run_backtest(world: World, raw_units: bool = False) -> BacktestResult:
    orders = sorted(world.orders.values(), key=lambda o: (o.delivery_date, o.order_time, o.order_id))
    res = BacktestResult()
    if not orders:
        res.notes.append("no delivered orders: more history is needed")
        return res
    first = orders[0].delivery_date
    tags_in = [{world.tag_of(s).tag_id for s in o.lines if world.tag_of(s)} for o in orders]
    spend_by_tag = []
    for o in orders:
        acc = defaultdict(float)
        for s, ln in o.lines.items():
            tag = world.tag_of(s)
            acc[tag.tag_id if tag else None] += ln.price
        spend_by_tag.append(acc)
    misses: dict[str, int] = defaultdict(int)
    paused: set[str] = set()
    rows = []
    for k, o in enumerate(orders):
        e = o.delivery_date
        before = [x for x in orders if x.delivery_date < e]
        placed = [Placed(x.order_id, "delivered", x.order_time, x.delivery_date, x.slot_end,
                         {s: ln.quantity for s, ln in x.lines.items()}, x, False, None, placement=x.order_time)
                  for x in before]
        state = CycleState(placed, [], Cycle(0, None, []), {p.order_id: p for p in placed})
        events = build_events(world, state, e, {})
        cadence = cadence_of([x.delivery_date for x in before], None)
        h_day = day_of(e) + cadence
        proposed, suggested = set(), set()
        for tag in sorted(world.tags.values(), key=lambda t: t.sort_key):
            if not tag.skus or tag.tag_id not in events:
                continue
            td = build_tag_data(world, tag, events[tag.tag_id], e, e, h_day, {}, {}, local_midnight(e))
            if td.size_unknown and len(td.window) >= GATE_SUGGEST:
                suggested.add(tag.tag_id)
                continue
            if not td.due:
                continue
            if td.n == GATE_SUGGEST:
                suggested.add(tag.tag_id)
            elif td.n >= GATE_ADD and tag.tag_id not in paused:
                proposed.add(tag.tag_id)
        bought = tags_in[k]
        nxt = tags_in[k + 1] if k + 1 < len(orders) else set()
        if e >= first + timedelta(days=MIN_HISTORY_DAYS):
            total = sum(spend_by_tag[k].values())
            hit = proposed & bought
            rows.append({
                "order_id": o.order_id, "date": e.isoformat(), "proposed": len(proposed), "suggested": len(suggested),
                "prec": len(hit) / len(proposed) if proposed else None,
                "prec_next": len(proposed & (bought | nxt)) / len(proposed) if proposed else None,
                "rec_lines": len(hit) / len(bought) if bought else 0.0,
                "rec_spend": sum(v for t, v in spend_by_tag[k].items() if t in proposed) / total if total else 0.0,
            })
        # Backtest-only pause heuristic: a tag the model would add that the order lacks counts as a miss.
        for g in proposed - bought:
            misses[g] += 1
            if misses[g] >= MISSES_TO_PAUSE:
                paused.add(g)
        for g in bought:
            misses[g] = 0
            paused.discard(g)
    res.per_order = rows
    res.evaluated = len(rows)
    if not rows:
        res.notes.append("no order is 183 days after the first one yet: more history is needed")
        return res
    res.proposed_per_order = statistics.mean(r["proposed"] for r in rows)
    res.suggestions_per_order = statistics.mean(r["suggested"] for r in rows)
    precs = [r["prec"] for r in rows if r["prec"] is not None]
    nexts = [r["prec_next"] for r in rows if r["prec_next"] is not None]
    res.precision_same = statistics.mean(precs) if precs else None
    res.precision_next = statistics.mean(nexts) if nexts else None
    res.recall_lines = statistics.mean(r["rec_lines"] for r in rows)
    res.recall_spend = statistics.mean(r["rec_spend"] for r in rows)
    if raw_units:
        res.notes.append("raw sale units (size 1 for every product)")
    return res


# ------------------------------------------------------------------------------------------ CSV loader
def _read(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _f(value) -> float | None:
    if value in (None, ""):
        return None
    return float(str(value).replace(",", "."))


def load_csv_world(directory: Path, raw_units: bool = False) -> tuple[World, list[str]]:
    """Load purchases.csv / products.csv / orders.csv into an in-memory database through the engine's API."""
    notes: list[str] = []
    purchases = _read(directory / "purchases.csv")
    products = {r["product_id"]: r for r in _read(directory / "products.csv")}
    totals = {}
    if (directory / "orders.csv").exists():
        totals = {r["order_id"]: _f(r.get("total_price_czk")) for r in _read(directory / "orders.csv")}
    by_order: dict[str, list[dict]] = defaultdict(list)
    for r in purchases:
        by_order[r["order_id"]].append(r)
    orders = []
    for oid in sorted(by_order, key=int):
        rows = by_order[oid]
        d = date.fromisoformat(rows[0]["delivered_date"])
        lines: dict[int, OrderLine] = {}
        for r in rows:
            sku = int(r["product_id"])
            q = _f(r["quantity"])
            price = _f(r["line_price_czk"]) or 0.0
            weighted = str(r.get("weighted", "")).lower() == "true"
            if sku in lines:  # duplicate lines add up
                ln = lines[sku]
                ln.quantity = (ln.quantity or 0.0) + (q or 0.0)
                ln.price += price
                ln.weighted = ln.weighted or weighted
            else:
                lines[sku] = OrderLine(sku, r["product_name"], q, None, r.get("textual_amount") or None, price, 0.0,
                                       weighted)
        orders.append(DeliveredOrder(int(oid), local_midnight(d), d, None, totals.get(oid), lines))
    now = local_midnight(max(o.delivery_date for o in orders) + timedelta(days=1))
    conn = connect(":memory:", now)
    with transaction(conn):
        store_delivered(conn, orders, now)
        reevaluate_auto_ignore(conn, now)
    # Tags: base unit kg if any SKU is weighed, else the majority unit of the parsed sizes; a tag whose
    # SKUs don't share one unit is loaded in raw sale units.
    skus_by_tag: dict[str, set[int]] = defaultdict(set)
    texts_by_sku: dict[int, list[str]] = defaultdict(list)
    weighted: dict[int, bool] = defaultdict(bool)
    for r in purchases:
        sku = int(r["product_id"])
        texts_by_sku[sku].append(r.get("textual_amount") or "")
        weighted[sku] |= str(r.get("weighted", "")).lower() == "true"
    names: dict[str, str] = {}
    truncated = []
    for pid, p in products.items():
        name = normalise_name(p["tag"])
        if len(name) > 64:
            truncated.append(name)
            name = name[:64].rstrip()
        names[pid] = name
        skus_by_tag[name].add(int(pid))
    if truncated:
        notes.append(f"{len(truncated)} tag name(s) longer than 64 characters truncated: {truncated}")
    ignored = {r[0] for r in conn.execute("SELECT sku FROM skus WHERE ignored = 1 ORDER BY sku")}
    latest_text = {r[0]: r[1] for r in conn.execute("SELECT sku, textual_amount FROM skus ORDER BY sku")}
    entries = []
    raw_tags = 0
    for name in sorted(skus_by_tag):
        # products.csv rows never purchased carry no history (and the engine has no SKU row for them)
        skus = sorted(s for s in skus_by_tag[name] if s not in ignored and s in texts_by_sku)
        if not skus:
            continue
        units = Counter()
        for s in skus:
            for t in texts_by_sku[s]:
                parsed = parse(t)
                if parsed:
                    units[parsed[1]] += 1
        heavy = any(weighted[s] for s in skus)
        if raw_units:
            unit, raw = "kg", True
        elif heavy:
            unit = "kg"
            raw = bool(set(units) - {"kg"})
        elif units:
            unit = sorted(units, key=lambda u: (-units[u], _UNIT_RANK[u]))[0]
            raw = len(units) > 1
        else:
            unit, raw = "pcs", False
        if raw and not raw_units:
            raw_tags += 1
            unit = "kg"
        for s in skus:
            storage = products[str(s)]["storage_class"]
            entry = {"sku": s, "product_tag": name, "policy": "cheapest", "base_unit": unit,
                     "storage_tag": STORAGE_MAP.get(storage, storage)}
            if raw:
                entry["units_per_pack"] = 1.0
            else:
                parsed = parse(latest_text.get(s))
                if parsed is None or parsed[1] != unit:
                    entry["units_per_pack"] = None
            entries.append(entry)
    tags_apply(conn, entries, "agent", now)
    if raw_tags:
        notes.append(f"{raw_tags} tag(s) whose products don't share one unit loaded in raw sale units")
    world = load_world(conn)
    conn.close()
    return world, notes


def pct(x: float | None) -> str:
    """A percentage, or "n/a" when it is undefined (no order with a proposal, §8.2/§13.4)."""
    return "n/a" if x is None else f"{x:.0%}"


def interpretation(res: BacktestResult) -> str:
    """One line for the setup report, e.g. 'about half of what I'd add you bought that week'."""
    if res.evaluated == 0:
        return "not enough history yet to backtest (it needs half a year)."
    if res.precision_same is None:
        return "I wouldn't have added anything yet, so there is nothing to score (precision n/a)."
    return (f"about {_words(res.precision_same)} of what I'd add you bought that week; "
            f"about {_words(res.precision_next)} within two orders.")


def _words(x: float | None) -> str:
    if x is None:
        return "none"
    tenths = round(x * 10)
    if tenths == 5:
        return "half"
    if tenths >= 10:
        return "all"
    if tenths <= 0:
        return "none"
    return f"{tenths} in 10"
