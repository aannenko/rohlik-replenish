"""History replay: misses, pauses and dropped variety types (§6.12); the once-per-sale wait (§6.14)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .constants import EPS, LINE_REASONS, MISSES_TO_PAUSE, TYPE_MISSES_TO_DROP, UNDATED_SALE_WAIT_DAYS
from .cycles import CycleState, Placed
from .world import World


@dataclass
class PauseInfo:
    since: datetime
    orders: list[int]


@dataclass
class ReplayResult:
    misses: dict[str, int] = field(default_factory=dict)
    paused: dict[str, PauseInfo] = field(default_factory=dict)
    type_misses: dict[tuple[str, int], int] = field(default_factory=dict)
    dropped: dict[tuple[str, int], PauseInfo] = field(default_factory=dict)
    added: dict[int, set[str]] = field(default_factory=dict)  # order_id -> A_k
    contained: dict[int, set[str]] = field(default_factory=dict)


def added_set(world: World, p: Placed) -> set[str]:
    """A_k: tags the agent added a line for in the cycle `p` closes (empty for an unmatched order)."""
    c = p.cycle
    out: set[str] = set()
    if c is None:
        return out
    for sku, q in c.owned.items():
        if q <= EPS:
            continue
        row = c.owner_row(sku)
        tag = world.tag_of(sku)
        if row is not None and row.reason in LINE_REASONS and tag is not None:
            out.add(tag.tag_id)
    for sku, (_, prop) in c.edited.items():
        row = prop.plan.get(sku)
        tag = world.tag_of(sku)
        if row is not None and row.reason in LINE_REASONS and prop.owned.get(sku, 0.0) > EPS and tag is not None:
            out.add(tag.tag_id)
    return out


def replay(world: World, state: CycleState) -> ReplayResult:
    res = ReplayResult()
    miss_orders: dict[str, list[int]] = {}
    type_orders: dict[tuple[str, int], list[int]] = {}
    for p in state.placed:  # by placement time, then order ID
        if p.cancelled:
            continue
        contained = set()
        for sku in p.units:
            tag = world.tag_of(sku)
            if tag is not None:
                contained.add(tag.tag_id)
        added = added_set(world, p)
        res.added[p.order_id] = added
        res.contained[p.order_id] = contained
        for g in sorted(added - contained):
            res.misses[g] = res.misses.get(g, 0) + 1
            miss_orders.setdefault(g, []).append(p.order_id)
            if res.misses[g] >= MISSES_TO_PAUSE and g not in res.paused:
                res.paused[g] = PauseInfo(p.placement, miss_orders[g][-MISSES_TO_PAUSE:])
        for g in contained:
            res.misses[g] = 0
            miss_orders[g] = []
            res.paused.pop(g, None)
        if p.cycle is not None:
            for sku in sorted(p.cycle.owned):
                tag = world.tag_of(sku)
                if tag is None or tag.policy != "variety" or p.cycle.offered(sku) <= EPS:
                    continue
                if p.qty(sku) <= EPS:
                    key = (tag.tag_id, sku)
                    res.type_misses[key] = res.type_misses.get(key, 0) + 1
                    type_orders.setdefault(key, []).append(p.order_id)
                    if res.type_misses[key] >= TYPE_MISSES_TO_DROP and key not in res.dropped:
                        res.dropped[key] = PauseInfo(p.placement, type_orders[key][-TYPE_MISSES_TO_DROP:])
        for sku in p.units:
            if p.qty(sku) <= EPS:
                continue
            tag = world.tag_of(sku)
            if tag is None:
                continue
            key = (tag.tag_id, sku)
            res.type_misses[key] = 0
            type_orders[key] = []
            res.dropped.pop(key, None)
    return res


def waiting_tags(world: World, state: CycleState, e: date, now: datetime) -> dict[str, date | None]:
    """Tags waiting after a written top-up until its sale ends (7 days after the order when undated)."""
    out: dict[str, date | None] = {}
    for c in state.closed:
        if c.closing.cancelled:
            continue
        for sku in sorted(c.owner):
            row = c.owner_row(sku)
            if row is None or row.stockup_units <= EPS or c.written_stockup(sku) <= EPS:
                continue
            tag = world.tag_of(sku)
            if tag is None:
                continue
            if row.sale_until is not None:
                if row.sale_until > e:
                    out[tag.tag_id] = max(row.sale_until, out.get(tag.tag_id) or row.sale_until)
            elif now - c.closing.placement < timedelta(days=UNDATED_SALE_WAIT_DAYS):
                out.setdefault(tag.tag_id, None)
    return out
