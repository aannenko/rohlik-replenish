"""Preferred products (§5.3, §6.13): the favourite by share, shared by the first run's guess and the 1 -> 2
migration's fill."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .constants import EPS, WINDOW_LONG, WINDOW_SHORT
from .sizes import delivered_line_base, size_of
from .timeutil import parse_iso


@dataclass(frozen=True)
class Purchase:
    """One event counted in n (§6.3), with the SKUs whose own line has a positive base quantity."""
    day: date
    placement: datetime
    skus: frozenset[int]


def purchase_counts(purchases: list[Purchase]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for p in purchases:
        for s in p.skus:
            counts[s] = counts.get(s, 0) + 1
    return counts


def favourite(purchases: list[Purchase]) -> int | None:
    """§6.13: the SKU with the largest share; ties go to the one bought most recently (its latest purchase, by
    delivery date, then placement time), then the lower SKU id."""
    counts = purchase_counts(purchases)
    latest: dict[int, tuple[date, datetime]] = {}
    for p in purchases:
        for s in p.skus:
            key = (p.day, p.placement)
            if s not in latest or key > latest[s]:
                latest[s] = key
    if not counts:
        return None
    return max(counts, key=lambda s: (counts[s], latest[s], -s))


def migration_fill(conn: sqlite3.Connection, e: date) -> dict[str, int | None]:
    """§5.3, for the 1 -> 2 migration: each `solo` tag's SKU with the largest share over the tag's window at the
    migration, without the guess's price condition; a tag without a purchase in its window gets the SKU it bought
    most recently, then the lower SKU id. A tag whose SKUs were never bought gets its lowest SKU id, and a tag
    without SKUs (which §5.3 deletes, so it can't occur) gets None.

    It reads the version-1 tables directly, before any world is loaded, and counts events as §6.2/§6.3 do: a
    delivered line's base quantity from its own text (else the SKU's latest), override and claims; an event with an
    unknown line isn't counted in n."""
    eligible = {r[0]: bool(r[1]) for r in conn.execute("SELECT name, stockup_eligible FROM storage_tags")}
    out: dict[str, int | None] = {}
    for tag_id, unit in conn.execute(
            "SELECT tag_id, base_unit FROM product_tags WHERE policy = 'solo' ORDER BY tag_id").fetchall():
        skus = {r[0]: r for r in conn.execute(
            "SELECT sku, textual_amount, weighted, storage_tag, upp_override, upp_unit FROM skus "
            "WHERE product_tag_id = ? ORDER BY sku", (tag_id,))}
        if not skus:
            out[tag_id] = None
            continue
        window = WINDOW_LONG if all(eligible.get(r[3], False) for r in skus.values()) else WINDOW_SHORT
        marks = ",".join("?" * len(skus))
        rows = conn.execute(
            "SELECT o.order_id, o.order_time, o.delivery_date, l.sku, l.quantity, l.weight_kg, l.textual_amount, "
            f"l.claimed_quantity FROM order_lines l JOIN orders o ON o.order_id = l.order_id WHERE l.sku IN ({marks}) "
            "ORDER BY o.delivery_date, o.order_time, o.order_id, l.sku", tuple(skus)).fetchall()
        orders: dict[int, list] = {}
        for r in rows:
            orders.setdefault(r[0], []).append(r)
        purchases: list[Purchase] = []
        bought: list[tuple[date, datetime, int]] = []
        start = e - timedelta(days=window)
        for lines in orders.values():
            day, placement = date.fromisoformat(lines[0][2]), parse_iso(lines[0][1])
            base: dict[int, float | None] = {}
            for r in lines:
                info = skus[r[3]]
                if (r[4] or 0) > 0 or (r[5] or 0) > 0:
                    bought.append((day, placement, r[3]))
                size = size_of(r[6] or info[1], unit, info[4], info[5], bool(info[2]))
                base[r[3]] = delivered_line_base(r[4], r[7], r[5], unit == "kg", size.value)
            if not (start < day < e) or any(b is None for b in base.values()) or sum(base.values()) <= EPS:
                continue
            purchases.append(Purchase(day, placement, frozenset(s for s, b in base.items() if b > EPS)))
        fav = favourite(purchases)
        if fav is None and bought:
            fav = max(bought, key=lambda x: (x[0], x[1], -x[2]))[2]
        out[tag_id] = fav if fav is not None else min(skus)
    return out


# Rows whose SKU was the tag's chosen candidate. A `solo` tag's row of this kind on its preferred product, without
# `fallback_from`, means the tag used its preferred product (§6.13 streak).
CHOICE_REASONS = frozenset({"due", "stockup", "wait_sale", "basket_limit", "budget_limit"})


def _tag_rows(world, tag, run) -> list:
    """A stored run's plan rows of a tag, through the current mapping (§5.3)."""
    return [r for s, r in run.plan.items() if s in world.skus and world.skus[s].tag_id == tag.tag_id]


def showed_fallback(world, tag, run) -> bool:
    """§2: whether a run bought the tag without its preferred product, or found it `unavailable` for want of it.
    Only such runs show the fallback reminder, so only they use up its showings. Line and `unavailable` rows are the
    only rows that record `fallback_from` (§6.13)."""
    return any(r.fallback_from == tag.preferred_sku for r in _tag_rows(world, tag, run))


def fallback_since(world, tag, now: datetime) -> datetime:
    """§6.13: the moment of a `solo` tag's fallback reminder, the start of the streak's first run: the earliest stored
    run with a row of the tag recording its preferred SKU (`fallback_from`) after the last run whose row of the tag
    used the preferred product; the current run (`now`) when no stored run has one. A row of another choice (another
    policy before, or another preferred product) ends the search too, so a new preferred product starts a new
    streak. Runs without rows of the tag don't matter."""
    start = now
    for run in reversed(world.runs):
        rows = _tag_rows(world, tag, run)
        if not rows:
            continue
        if showed_fallback(world, tag, run):
            start = run.started_at
        elif any(r.fallback_from is not None or r.reason in CHOICE_REASONS for r in rows):
            break
    return start
