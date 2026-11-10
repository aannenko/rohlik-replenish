"""Durable state loaded into plain records for planning (spec §3.3: derived state is recomputed)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from .db import get_meta
from .timeutil import parse_iso


@dataclass
class SkuInfo:
    sku: int
    name: str
    text: str | None
    weighted: bool
    ignored: bool
    tag_id: str | None
    storage: str | None
    assigned_by: str | None
    upp: float | None
    upp_unit: str | None
    upp_source: str | None
    updated_at: datetime


@dataclass
class TagInfo:
    tag_id: str
    name: str
    policy: str
    base_unit: str
    stockup_mode: str | None
    note: str | None
    source: str
    preferred_sku: int | None = None  # `solo` tags only (§5.3)
    preferred_source: str | None = None
    skus: list[int] = field(default_factory=list)
    long_life: bool = False
    never: bool = False

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.name, self.tag_id)


@dataclass
class DeliveredLine:
    sku: int
    quantity: float | None
    weight_kg: float | None
    text: str | None
    price: float
    claimed: float


@dataclass
class CachedOrder:
    order_id: int
    order_time: datetime
    delivery_date: date
    slot_end: datetime | None
    total: float | None
    lines: dict[int, DeliveredLine]


@dataclass
class PlanRowRec:
    sku: int
    reason: str
    normal_units: float
    stockup_units: float
    sale_until: date | None
    size: float | None
    size_unit: str | None
    fallback_from: int | None = None  # `solo`: the preferred SKU this row stands in for (§6.13)


@dataclass
class CartRowRec:
    sku: int
    before_qty: float
    user_owned_before: float
    target_qty: float | None
    after_qty: float | None
    agent_owned: float | None


@dataclass
class UpcomingRec:
    order_id: int
    order_time: datetime | None
    delivery_date: date
    slot_end: datetime | None
    amounts: dict[int, float]


@dataclass
class RunRecord:
    run_id: str
    seq: int
    started_at: datetime
    finished_at: datetime | None
    state: str  # dry | writing | done | failed | current
    recovered: bool
    t_date: date | None
    cadence: float | None
    options: dict
    plan: dict[int, PlanRowRec] = field(default_factory=dict)
    cart: dict[int, CartRowRec] = field(default_factory=dict)
    edits: dict[int, str] = field(default_factory=dict)  # sku -> proposal run id
    upcoming: dict[int, UpcomingRec] = field(default_factory=dict)
    owned: dict[int, float] = field(default_factory=dict)  # final or provisional agent ownership
    after: dict[int, float] = field(default_factory=dict)

    @property
    def non_dry(self) -> bool:
        return self.state in ("writing", "done")

    @property
    def crashed(self) -> bool:
        return self.state == "writing" or (self.state == "done" and self.recovered and self.finished_at is None)

    @property
    def end_time(self) -> datetime:
        """Its finish, or its start if it crashed (a crashed run's finished_at stays NULL)."""
        return self.finished_at or self.started_at

    @property
    def order_key(self) -> tuple[datetime, int]:
        return (self.started_at, self.seq)


@dataclass
class World:
    skus: dict[int, SkuInfo]
    tags: dict[str, TagInfo]
    eligible: dict[str, bool]
    orders: dict[int, CachedOrder]
    cancelled: set[int]
    runs: list[RunRecord]  # every stored run, by (started_at, seq)
    setup_completed_at: datetime | None
    cadence_override: float | None
    preferred_check: bool = False  # the 1 -> 2 migration set preferred products, for the user to check (§8.1)

    def tag_of(self, sku: int) -> TagInfo | None:
        info = self.skus.get(sku)
        if info is None or info.tag_id is None:
            return None
        return self.tags.get(info.tag_id)

    def display_name(self, sku: int, tag: TagInfo | None = None) -> str:
        """§8.1: a product's name as in Rohlik, followed by its pack text in brackets where another product of its
        tag shares the name (e.g. two pack sizes), so the user can tell which one is meant."""
        info = self.skus.get(sku)
        if info is None:
            return f"#{sku}"
        tag = tag if tag is not None else self.tag_of(sku)
        if tag is not None and info.text and any(
                s != sku and s in self.skus and self.skus[s].name == info.name for s in tag.skus):
            return f"{info.name} ({info.text})"
        return info.name

    def run(self, run_id: str) -> RunRecord | None:
        for r in self.runs:
            if r.run_id == run_id:
                return r
        return None


def run_seq(run_id: str) -> int:
    try:
        return int(run_id[1:7])
    except ValueError:
        return 0


def _d(value) -> date | None:
    return date.fromisoformat(value) if value else None


def _t(value) -> datetime | None:
    return parse_iso(value) if value else None


def provisional_owned(run: RunRecord) -> dict[int, float]:
    """§6.15 step 1: what a crashed `writing` run provisionally owns, per SKU."""
    owned = {}
    for sku, row in run.cart.items():
        if row.agent_owned is not None:
            owned[sku] = row.agent_owned
        elif row.target_qty is not None and abs(row.target_qty - row.before_qty) > 1e-9:
            owned[sku] = max(0.0, row.target_qty - row.user_owned_before)
        else:
            owned[sku] = max(0.0, row.before_qty - row.user_owned_before)
    return owned


def load_world(conn: sqlite3.Connection) -> World:
    eligible = {r[0]: bool(r[1]) for r in conn.execute(
        "SELECT name, stockup_eligible FROM storage_tags ORDER BY name")}
    tags: dict[str, TagInfo] = {}
    for r in conn.execute("SELECT * FROM product_tags ORDER BY name, tag_id"):
        tags[r["tag_id"]] = TagInfo(r["tag_id"], r["name"], r["policy"], r["base_unit"], r["stockup_mode"],
                                    r["note"], r["source"], r["preferred_sku"], r["preferred_source"])
    for r in conn.execute("SELECT tag_id FROM prefs WHERE kind = 'never' ORDER BY tag_id"):
        if r[0] in tags:
            tags[r[0]].never = True
    skus: dict[int, SkuInfo] = {}
    for r in conn.execute("SELECT * FROM skus ORDER BY sku"):
        info = SkuInfo(r["sku"], r["name"], r["textual_amount"], bool(r["weighted"]), bool(r["ignored"]),
                       r["product_tag_id"], r["storage_tag"], r["assigned_by"], r["upp_override"], r["upp_unit"],
                       r["upp_source"], parse_iso(r["updated_at"]))
        skus[info.sku] = info
        if info.tag_id in tags:
            tags[info.tag_id].skus.append(info.sku)
    for t in tags.values():
        t.skus.sort()
        t.long_life = bool(t.skus) and all(eligible.get(skus[s].storage or "", False) for s in t.skus)
    orders: dict[int, CachedOrder] = {}
    for r in conn.execute("SELECT * FROM orders ORDER BY order_id"):
        orders[r["order_id"]] = CachedOrder(r["order_id"], parse_iso(r["order_time"]), date.fromisoformat(
            r["delivery_date"]), _t(r["slot_end"]), r["total_czk"], {})
    for r in conn.execute("SELECT * FROM order_lines ORDER BY order_id, sku"):
        orders[r["order_id"]].lines[r["sku"]] = DeliveredLine(r["sku"], r["quantity"], r["weight_kg"],
                                                              r["textual_amount"], r["line_price_czk"],
                                                              r["claimed_quantity"])
    cancelled = {r[0] for r in conn.execute("SELECT order_id FROM cancelled_orders ORDER BY order_id")}
    runs: dict[str, RunRecord] = {}
    for r in conn.execute("SELECT * FROM runs ORDER BY started_at, run_id"):
        runs[r["run_id"]] = RunRecord(r["run_id"], run_seq(r["run_id"]), parse_iso(r["started_at"]),
                                      _t(r["finished_at"]), r["state"], bool(r["recovered"]), _d(r["t_date"]),
                                      r["cadence_days"], json.loads(r["options"] or "{}"))
    for r in conn.execute("SELECT * FROM run_plan ORDER BY run_id, sku"):
        runs[r["run_id"]].plan[r["sku"]] = PlanRowRec(r["sku"], r["reason"], r["normal_units"], r["stockup_units"],
                                                      _d(r["sale_until"]), r["size"], r["size_unit"],
                                                      r["fallback_from"])
    for r in conn.execute("SELECT * FROM run_cart ORDER BY run_id, sku"):
        runs[r["run_id"]].cart[r["sku"]] = CartRowRec(r["sku"], r["before_qty"], r["user_owned_before"],
                                                      r["target_qty"], r["after_qty"], r["agent_owned"])
    for r in conn.execute("SELECT * FROM run_edits ORDER BY run_id, sku"):
        runs[r["run_id"]].edits[r["sku"]] = r["proposal_run"]
    for r in conn.execute("SELECT * FROM run_upcoming_orders ORDER BY run_id, order_id, sku"):
        rec = runs[r["run_id"]].upcoming.get(r["order_id"])
        if rec is None:
            rec = UpcomingRec(r["order_id"], _t(r["order_time"]), date.fromisoformat(r["delivery_date"]),
                              _t(r["slot_end"]), {})
            runs[r["run_id"]].upcoming[r["order_id"]] = rec
        rec.amounts[r["sku"]] = r["amount"]
    ordered = sorted(runs.values(), key=lambda x: x.order_key)
    for run in ordered:
        if run.state == "writing":
            run.owned = provisional_owned(run)
        else:
            run.owned = {s: (row.agent_owned or 0.0) for s, row in run.cart.items()}
        run.after = {s: row.after_qty for s, row in run.cart.items() if row.after_qty is not None}
    setup = get_meta(conn, "setup_completed_at")
    cadence = get_meta(conn, "cadence_days")
    return World(skus, tags, eligible, orders, cancelled, ordered, parse_iso(setup) if setup else None,
                 float(cadence) if cadence else None, get_meta(conn, "preferred_check") is not None)
