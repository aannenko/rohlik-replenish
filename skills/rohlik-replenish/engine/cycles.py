"""Placed orders, cycles and matching (§6.6); ownership at close, origin and written stock-up (§6.7);
cart ownership before planning, edits and crash recovery (§6.15 steps 1-4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .constants import CHECKOUT_PENDING_HOURS, EPS, LINE_REASONS
from .numerics import clamp
from .world import CachedOrder, RunRecord, UpcomingRec, World


@dataclass
class Placed:
    order_id: int
    source: str  # delivered | upcoming (current read) | recorded (seen by some run only)
    order_time: datetime | None
    delivery_date: date
    slot_end: datetime | None
    units: dict[int, float | None]
    cached: CachedOrder | None
    cancelled: bool
    first_sighting: RunRecord | None
    vanished: str | None = None  # kept | dropped (unexplained, §6.1)
    placement: datetime | None = None
    ref_run: RunRecord | None = None
    cycle: "Cycle | None" = None  # the cycle this order closes (None when unmatched)

    @property
    def delivered(self) -> bool:
        return self.cached is not None

    @property
    def matched(self) -> RunRecord | None:
        return self.cycle.last if self.cycle else None

    @property
    def active(self) -> bool:
        """Placed, not cancelled and not a dropped vanished order: adds stock/events, counts for misses."""
        return not self.cancelled and self.vanished != "dropped"

    def has_line(self, sku: int) -> bool:
        return sku in self.units

    def qty(self, sku: int) -> float:
        return self.units.get(sku) or 0.0

    @property
    def key(self) -> tuple[datetime, int]:
        return (self.placement, self.order_id)


@dataclass
class Cycle:
    index: int
    closing: Placed | None
    runs: list[RunRecord]
    owned: dict[int, float] = field(default_factory=dict)
    owner: dict[int, RunRecord] = field(default_factory=dict)
    edited: dict[int, tuple[RunRecord, RunRecord]] = field(default_factory=dict)  # sku -> (detecting, proposal)

    @property
    def last(self) -> RunRecord | None:
        return self.runs[-1] if self.runs else None

    def run_ids(self) -> set[str]:
        return {r.run_id for r in self.runs}

    def owner_row(self, sku: int):
        run = self.owner.get(sku)
        return run.plan.get(sku) if run else None

    def offered(self, sku: int) -> float:
        row = self.owner_row(sku)
        if row is not None and row.reason in LINE_REASONS:
            return self.owned.get(sku, 0.0)
        return 0.0

    def written_stockup(self, sku: int) -> float:
        row = self.owner_row(sku)
        if row is None:
            return 0.0
        return max(0.0, min(row.stockup_units, self.owned.get(sku, 0.0) - row.normal_units))

    def taken_stockup(self, sku: int, order_units: float) -> float:
        row = self.owner_row(sku)
        if row is None or row.stockup_units <= EPS:
            return 0.0
        return max(0.0, order_units - row.normal_units)


@dataclass
class CycleState:
    placed: list[Placed]  # by (placement, order_id)
    closed: list[Cycle]
    current: Cycle
    by_order: dict[int, Placed]


def proposing_run(runs: list[RunRecord], sku: int, default: RunRecord) -> RunRecord:
    """The latest run among `runs` with a `due` or `stockup` row for `sku`, else `default`."""
    for r in reversed(runs):
        row = r.plan.get(sku)
        if row is not None and row.reason in LINE_REASONS:
            return r
    return default


def build_placed(world: World, current_upcoming: list[UpcomingRec], current: RunRecord, e: date) -> list[Placed]:
    sightings: dict[int, list[tuple[RunRecord, UpcomingRec]]] = {}
    for run in world.runs:
        for oid in sorted(run.upcoming):
            sightings.setdefault(oid, []).append((run, run.upcoming[oid]))
    for rec in sorted(current_upcoming, key=lambda r: r.order_id):
        sightings.setdefault(rec.order_id, []).append((current, rec))
    placed: dict[int, Placed] = {}
    for oid in sorted(world.orders):
        o = world.orders[oid]
        first = sightings[oid][0][0] if oid in sightings else None
        placed[oid] = Placed(oid, "delivered", o.order_time, o.delivery_date, o.slot_end,
                             {s: ln.quantity for s, ln in o.lines.items()}, o, oid in world.cancelled, first)
    current_ids = {r.order_id for r in current_upcoming}
    for oid in sorted(sightings):
        if oid in placed:
            continue
        sights = sightings[oid]
        last_run, last = sights[-1]
        order_time = next((rec.order_time for _, rec in reversed(sights) if rec.order_time), None)
        source = "upcoming" if oid in current_ids else "recorded"
        p = Placed(oid, source, order_time, last.delivery_date, last.slot_end, dict(last.amounts), None,
                   oid in world.cancelled, sights[0][0])
        if source == "recorded" and not p.cancelled:
            p.vanished = "kept" if last.delivery_date >= e else "dropped"
        placed[oid] = p
    return [placed[k] for k in sorted(placed)]


def resolve_cycles(world: World, placed: list[Placed]) -> CycleState:
    non_dry = [r for r in world.runs if r.non_dry]
    known = [p for p in placed if p.order_time is not None]
    for p in known:
        p.placement = p.order_time
    # Placement inference (§6.6): one order at a time, by first sighting, then order ID.
    unknown = sorted((p for p in placed if p.order_time is None),
                     key=lambda p: (p.first_sighting.order_key, p.order_id))
    inferred: list[Placed] = []
    for p in unknown:
        sighting = p.first_sighting
        prior = [q.placement for q in known if q.placement < sighting.started_at] + [q.placement for q in inferred]
        p_prev = max(prior) if prior else None
        contained = [s for s, u in p.units.items() if (u or 0) > EPS]
        candidates = [r for r in non_dry if r.order_key < sighting.order_key
                      and (p_prev is None or r.started_at > p_prev)
                      and any(r.owned.get(s, 0.0) > EPS for s in contained)]
        if candidates:
            p.ref_run = candidates[-1]
            p.placement = p.ref_run.end_time
        else:
            p.placement = sighting.started_at
        inferred.append(p)
    ordered = sorted(placed, key=lambda p: p.key)
    assigned: dict[str, Cycle] = {}
    closed: list[Cycle] = []
    prev: datetime | None = None
    for p in ordered:
        members = [r for r in non_dry if r.run_id not in assigned and (prev is None or r.started_at >= prev)
                   and r.started_at < p.placement]
        if p.ref_run is not None and p.ref_run.run_id not in assigned and p.ref_run not in members:
            members.append(p.ref_run)  # the reference run always belongs to the cycle it closes
        members.sort(key=lambda r: r.order_key)
        if members:
            cycle = Cycle(len(closed), p, members)
            closed.append(cycle)
            for r in members:
                assigned[r.run_id] = cycle
            p.cycle = cycle
        prev = p.placement
    current = Cycle(len(closed), None, [r for r in non_dry if r.run_id not in assigned])
    return CycleState(ordered, closed, current, {p.order_id: p for p in ordered})


def close_ownership(world: World, state: CycleState) -> None:
    """Ownership at close, owner runs and edits per closed cycle (§6.7). Runs must hold final ownership."""
    by_id = {r.run_id: r for r in world.runs}
    for c in state.closed:
        last = c.last
        ids = c.run_ids()
        owned = {s: q for s, q in last.owned.items()}
        edited: dict[int, tuple[RunRecord, RunRecord]] = {}
        for r in c.runs:
            for sku in sorted(r.edits):
                prop = by_id.get(r.edits[sku])
                if prop is not None and prop.run_id in ids:  # a proposal outside the cycle isn't this cycle's
                    edited[sku] = (r, prop)
        for sku, (_, prop) in edited.items():
            owned[sku] = prop.owned.get(sku, 0.0)
        owner: dict[int, RunRecord] = {}
        for sku in sorted(set(owned) | set(edited)):
            if sku in edited:
                owner[sku] = edited[sku][1]
            else:
                for r in reversed(c.runs):
                    row = r.plan.get(sku)
                    if row is not None and row.reason in LINE_REASONS:
                        owner[sku] = r
                        break
        c.owned, c.owner, c.edited = owned, owner, edited


def is_agent_only(p: Placed, tag_skus: list[int]) -> bool:
    """Origin (§6.7) of the event of a tag in a placed order."""
    if p.cycle is None:
        return False
    for s in tag_skus:
        if not p.has_line(s):
            continue
        units = p.units.get(s)
        if units is None:
            return False
        if units > p.cycle.owned.get(s, 0.0) + EPS:
            return False
    return True


def is_choice(p: Placed, tag_skus: list[int]) -> bool:
    """A choice event: the user chose the tag's contents (§6.7)."""
    if p.cycle is None:
        return True
    for s in tag_skus:
        if p.has_line(s) or p.cycle.owned.get(s, 0.0) > EPS:
            if abs(p.qty(s) - p.cycle.owned.get(s, 0.0)) > EPS:
                return True
    return False


# ------------------------------------------------------------------ before planning (§6.15)
def checkout_pending(world: World, state: CycleState, cart: dict[int, float], now: datetime) -> RunRecord | None:
    non_dry = [r for r in world.runs if r.non_dry]
    if not non_dry:
        return None
    last = non_dry[-1]
    if last.run_id not in state.current.run_ids():
        return None
    owned = [s for s, q in last.owned.items() if q > EPS]
    if not owned:
        return None
    if any(q > EPS for q in cart.values()):  # every owned SKU left the cart, and nothing else is in it
        return None
    if now - last.end_time >= timedelta(hours=CHECKOUT_PENDING_HOURS):
        return None
    return last


def checkout_in_progress(world: World, state: CycleState, cart: dict[int, float],
                         current: RunRecord) -> tuple[RunRecord, Placed] | None:
    """§6.6: only an upcoming order first sighted by this run can be a checkout still in flight; an order an
    earlier run already recorded is an ordinary placed order, whatever the cart now holds."""
    non_dry = [r for r in world.runs if r.non_dry]
    for p in sorted((p for p in state.placed if p.source == "upcoming"), key=lambda p: p.key, reverse=True):
        if p.first_sighting is not current:
            continue
        # the last non-dry run that started before the order's placement; an inferred placement is its reference
        # run's finish, so that run counts even when it took no time (injected clocks)
        before = [r for r in non_dry if r.started_at < p.placement or r is p.ref_run]
        if not before:
            continue
        last = max(before, key=lambda r: r.order_key)
        owned = {s: q for s, q in last.owned.items() if q > EPS}
        if not owned or p.order_id in last.upcoming:
            continue
        if all(p.qty(s) >= q - EPS for s, q in owned.items()) and all(
                cart.get(s, 0.0) >= q - EPS for s, q in owned.items()):
            return last, p
    return None


@dataclass
class Ownership:
    last: RunRecord | None
    agent_owned: dict[int, float]
    user_owned: dict[int, float]
    new_edits: dict[int, RunRecord]  # sku -> proposing run
    locked_skus: set[int]


def current_ownership(state: CycleState, cart: dict[int, float], detect: bool) -> Ownership:
    cycle = state.current
    last = cycle.last
    new_edits: dict[int, RunRecord] = {}
    agent: dict[int, float] = {}
    if last is not None:
        for sku in sorted(last.owned):
            q = last.owned[sku]
            if q <= EPS:
                continue
            if detect and abs(cart.get(sku, 0.0) - last.after.get(sku, 0.0)) > EPS:
                new_edits[sku] = proposing_run(cycle.runs, sku, last)
            else:
                agent[sku] = q
    ids = cycle.run_ids()
    locked = {sku for r in cycle.runs for sku, prop in r.edits.items() if prop in ids} | set(new_edits)
    user = {sku: max(0.0, cart.get(sku, 0.0) - agent.get(sku, 0.0)) for sku in sorted(set(cart) | set(agent))}
    return Ownership(last, agent, user, new_edits, locked)


def recovery_values(run: RunRecord, x: dict[int, float]) -> dict[int, tuple[float, float]]:
    """§6.15 step 2: final (after_qty, agent_owned) of a crashed run, given x."""
    out = {}
    for sku, row in sorted(run.cart.items()):
        if row.after_qty is not None:
            out[sku] = (row.after_qty, row.agent_owned if row.agent_owned is not None else 0.0)
        elif row.target_qty is not None and abs(row.target_qty - row.before_qty) > EPS:
            after = x.get(sku, 0.0)
            hi = max(row.target_qty, row.before_qty) - row.user_owned_before
            out[sku] = (after, clamp(after - row.user_owned_before, 0.0, max(0.0, hi)))
        else:
            out[sku] = (row.before_qty, max(0.0, row.before_qty - row.user_owned_before))
    return out
