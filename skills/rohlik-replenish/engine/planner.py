"""Plan building (§6.0-§6.14): events, rates, projections, classification, amounts, budget."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .choice import Allocation, Allocator, Cand, Live, cheapest_order, qualifying_head, topup_value
from .constants import (CADENCE_DEFAULT, CADENCE_GAPS, CADENCE_MAX, CADENCE_MIN, EPS, FIT_FACTOR, GATE_ADD,
                        GATE_SUGGEST, LINE_REASONS, OVERDUE_FACTOR, P90, SOLO_GUESS_MIN_SKUS, SOLO_GUESS_SHARE,
                        VARIETY_CHOICE_EVENTS, VARIETY_FLOOR, VARIETY_ROTATION_ORDERS, WINDOW_LONG, WINDOW_SHORT)
from .cycles import CycleState, Ownership, Placed, is_agent_only, is_choice
from .numerics import median, percentile_nearest_rank
from .preferred import Purchase, favourite, purchase_counts
from .replay import ReplayResult
from .sizes import Size, delivered_line_base, gross_line_base, size_of
from .stock import Projection, RateHistory, StockEvent, day_of, project
from .timeutil import end_of_day, local_date
from .world import RunRecord, TagInfo, World


# ---------------------------------------------------------------------------------------------- events
@dataclass
class Ev:
    order: Placed | None  # None for the cart
    date: date
    placement: datetime
    order_id: int
    q: float | None
    spend: float
    units: dict[int, float | None]
    base: dict[int, float | None]
    history: bool
    user: bool
    stockup_base: float
    choice: bool
    arrival_time: datetime

    @property
    def day(self) -> float:
        return day_of(self.date)

    def stock_event(self, e_day: float | None = None) -> StockEvent:
        day = self.day if e_day is None else max(self.day, e_day)
        return StockEvent(day, self.placement, self.order_id, self.q or 0.0, self.stockup_base, self.user,
                          self.arrival_time)


def current_size(world: World, sku: int, unit: str, live: Live | None) -> Size:
    """Current size: size(s, live textualAmount), else size(s, skus.textual_amount) (§6.2)."""
    info = world.skus[sku]
    if live is not None and live.text:
        s = size_of(live.text, unit, info.upp, info.upp_unit, info.weighted)
        if s.known:
            return s
    return size_of(info.text, unit, info.upp, info.upp_unit, info.weighted)


def line_size(world: World, sku: int, unit: str, line_text: str | None) -> Size:
    info = world.skus[sku]
    return size_of(line_text or info.text, unit, info.upp, info.upp_unit, info.weighted)


def build_events(world: World, state: CycleState, e: date, live: dict[int, Live]) -> dict[str, list[Ev]]:
    """Every event of every tag, in placed (non-cancelled) orders (§6.3)."""
    out: dict[str, list[Ev]] = {}
    for p in state.placed:
        if not p.active:
            continue
        groups: dict[str, list[int]] = {}
        for sku in p.units:
            tag = world.tag_of(sku)
            if tag is not None:
                groups.setdefault(tag.tag_id, []).append(sku)
        for tag_id, skus in groups.items():
            tag = world.tags[tag_id]
            units, base = {}, {}
            spend = 0.0
            for sku in sorted(skus):
                units[sku] = p.units.get(sku)
                if p.delivered:
                    ln = p.cached.lines[sku]
                    size = line_size(world, sku, tag.base_unit, ln.text)
                    base[sku] = delivered_line_base(ln.quantity, ln.claimed, ln.weight_kg, tag.base_unit == "kg",
                                                    size.value)
                    spend += ln.price
                else:
                    size = current_size(world, sku, tag.base_unit, live.get(sku))
                    u = units[sku]
                    base[sku] = None if (u is None or size.value is None) else u * size.value
            q = None if any(v is None for v in base.values()) else sum(base.values())
            stock = 0.0
            if p.cycle is not None:
                for sku in skus:
                    u = units.get(sku) or 0.0
                    taken = p.cycle.taken_stockup(sku, u)
                    if taken > EPS and u > EPS and base.get(sku) is not None:
                        stock += taken * base[sku] / u
            arrival = p.slot_end or end_of_day(p.delivery_date)
            if not p.delivered and p.delivery_date < e:
                arrival = max(arrival, end_of_day(e))  # an overdue delivery is still on its way, not on the shelf
            out.setdefault(tag_id, []).append(Ev(
                p, p.delivery_date, p.placement, p.order_id, q, spend, units, base,
                p.delivered and p.delivery_date < e, not is_agent_only(p, tag.skus), stock,
                is_choice(p, tag.skus), arrival))
    for evs in out.values():
        evs.sort(key=lambda ev: (ev.date, ev.placement, ev.order_id))
    return out


# ---------------------------------------------------------------------------------------------- tag data
@dataclass
class TagData:
    tag: TagInfo
    window_days: int
    sizes: dict[int, Size]
    history: list[Ev]
    incoming: list[Ev]
    window: list[Ev]
    n: int
    size_unknown: bool
    unknown_notes: list[str]
    interval: tuple[float, float] | None
    rate: float | None
    managed: bool
    usual_batch: float | None
    max_level: float | None
    p90: float | None
    cart_units: dict[int, float]  # user-owned cart units of the tag's SKUs
    projection: Projection | None = None
    last_purchase: date | None = None
    usual_gap: float | None = None

    @property
    def due(self) -> bool:
        return bool(self.projection and self.projection.due)


def build_tag_data(world: World, tag: TagInfo, events: list[Ev], e: date, t: date, h_day: float,
                   live: dict[int, Live], user_owned: dict[int, float], now: datetime,
                   raw_units: bool = False, cart_skus: set[int] | frozenset = frozenset()) -> TagData:
    """`user_owned` feeds the cart's reset event (§6.9); `cart_skus` is every SKU in the cart, agent-owned lines
    included, for the size-unknown gate (§6.2: any cart line of the tag)."""
    w = WINDOW_LONG if tag.long_life else WINDOW_SHORT
    hist = [ev for ev in events if ev.history]
    incoming = [ev for ev in events if not ev.history]
    window = [ev for ev in hist if ev.date > e - timedelta(days=w)]
    sizes = {s: current_size(world, s, tag.base_unit, live.get(s)) for s in tag.skus}
    notes: list[str] = []
    unknown = False
    if any(ev.q is None for ev in window) or any(ev.q is None for ev in incoming):
        unknown = True
    cart_units = {s: user_owned.get(s, 0.0) for s in tag.skus if user_owned.get(s, 0.0) > EPS}
    for s in tag.skus:
        if (s in cart_units or s in cart_skus) and not sizes[s].known:
            unknown = True
    if unknown:
        for s in tag.skus:
            sz = sizes[s]
            if not sz.known:
                if sz.note == "other_unit":
                    notes.append(f"size in another unit: {sz.text}")
                else:
                    notes.append(f"pack size unknown: {world.display_name(s, tag)}")
    pos_window = [ev for ev in window if ev.q is not None and ev.q > EPS]
    pos_hist = [ev for ev in hist if ev.q is not None and ev.q > EPS]
    n = len(pos_window)
    hist_stock = [ev.stock_event() for ev in hist if ev.q is not None]
    rates = RateHistory(hist_stock, w)
    interval = rates.interval(day_of(e))
    rate = rates.rate(day_of(e))
    if rate is not None and rate <= EPS:
        rate = None
    managed = tag.long_life and tag.stockup_mode is not None and len(window) >= 1
    usual = max_level = p90 = None
    if pos_window:
        qs = [ev.q for ev in pos_window]
        if tag.long_life:
            usual = sum(qs) / len(qs)
            max_level = max(qs)
        else:
            p90 = percentile_nearest_rank(qs, P90)
    td = TagData(tag, w, sizes, hist, incoming, window, n, unknown, notes, interval, rate, managed, usual,
                 max_level, p90, cart_units)
    if rate is not None:
        extra = []
        if cart_units:
            base = 0.0
            known = True
            for s, u in cart_units.items():
                if sizes[s].known:
                    base += u * sizes[s].value
                else:
                    known = False
            if known and base > EPS:
                extra.append(StockEvent(day_of(t), now, 2**62, base, 0.0, True, end_of_day(t)))
        inc = [ev.stock_event(day_of(e)) for ev in incoming if ev.q is not None]
        td.projection = project(hist_stock, inc, rate, day_of(e), day_of(t), h_day, extra, past=rates)
    pos_all = pos_hist + [ev for ev in incoming if ev.q is not None and ev.q > EPS]
    if pos_all:
        td.last_purchase = max(ev.date for ev in pos_all)
    if interval:
        a, b = interval
        k = len({ev.day for ev in pos_hist if a <= ev.day <= b})
        if k >= 2:
            td.usual_gap = (b - a) / (k - 1)
    return td


# ---------------------------------------------------------------------------------------------- planning
@dataclass
class PlanRow:
    tag_id: str
    sku: int
    reason: str
    normal_units: int = 0
    stockup_units: int = 0
    sale_until: date | None = None
    size: float | None = None
    size_unit: str | None = None
    fallback_from: int | None = None  # `solo`: the preferred SKU this row stands in for (§6.13)


@dataclass
class TagPlan:
    tag: TagInfo
    td: TagData | None
    category: str = "none"  # none|never|paused|locked|suggestion|line|unavailable
    reason: str | None = None
    rows: list[PlanRow] = field(default_factory=list)
    alloc: Allocation | None = None
    cands: dict[int, Cand] = field(default_factory=dict)
    ordered: list[Cand] = field(default_factory=list)
    pool: list[Cand] = field(default_factory=list)
    wants: str | None = None  # line|topup|wait_sale
    kind: str | None = None  # short|always|on_sale
    fallback_from: int | None = None  # solo: the preferred SKU, while the tag buys without it or is unavailable
    fallback_too_big: bool = False  # ... it was available, but the fit rule dropped it (§6.13)
    preferred_issue: str | None = None  # ... why it isn't a candidate, e.g. Rohlik's availability text
    normal_amount: float = 0.0
    note: str | None = None
    rep_sku: int | None = None
    saved: float = 0.0
    extra: float = 0.0
    budget_trimmed: bool = False
    unavailable_reason: str | None = None
    reminder_since: date | None = None  # an `overdue` suggestion's date (§2 reminders)

    @property
    def due(self) -> bool:
        return bool(self.td and self.td.due)


@dataclass
class Assessment:
    mode: str
    on_sale_units: float
    total_units: float
    from_prices: bool = True  # False: no usable line (first run), or the default `always` of a later run (§6.14)


@dataclass
class Guess:
    """§6.13 preferred-product guess (first run only): the favourite and its evidence."""
    sku: int
    purchases: int
    total: int
    paid: float
    others_mean: float

    @property
    def pricier_pct(self) -> int:
        return int(math.floor((self.paid / self.others_mean - 1.0) * 100 + 0.5))


@dataclass
class PlanInput:
    world: World
    state: CycleState
    own: Ownership
    e: date
    t: date
    now: datetime
    cadence: float
    live: dict[int, Live]
    cart_limits: dict[int, float]  # cart line maxBasketAmount
    replay: ReplayResult
    waiting: dict[str, date | None]
    min_discount: float
    budget: float
    first_run: bool = False  # only the first run assesses stock-up modes from prices (§6.14)


@dataclass
class PlanResult:
    plans: list[TagPlan]
    rows: list[PlanRow]
    assessed: dict[str, Assessment]
    locked_tags: set[str]
    events: dict[str, list[Ev]]
    h_day: float
    kept_tags: set[str] = field(default_factory=set)  # §6.11 rule 3: an agent-owned SKU got no live data
    guesses: dict[str, Guess] = field(default_factory=dict)  # §6.13: the first run's solo guesses, by tag_id


def _history_skus(world: World, e: date) -> set[int]:
    return {s for o in world.orders.values() if o.delivery_date < e for s, ln in o.lines.items()
            if (ln.quantity or 0) > 0 or (ln.weight_kg or 0) > 0}


def _last_placed(state: CycleState) -> dict[int, datetime]:
    out: dict[int, datetime] = {}
    for p in state.placed:
        if p.cancelled:
            continue
        for s in p.units:
            if p.qty(s) > EPS:
                out[s] = max(out.get(s, p.placement), p.placement)
    return out


def assess(world: World, tag: TagInfo, td: TagData, live: dict[int, Live], min_discount: float) -> Assessment:
    """§6.14: paid price vs today's regular price per base unit, by gross base quantity before claims."""
    on_base = tot_base = on_units = tot_units = 0.0
    for ev in td.window:
        if ev.order is None or ev.order.cached is None:
            continue
        for sku, ln in ev.order.cached.lines.items():
            if sku not in tag.skus or ln.price <= 0:
                continue
            lv = live.get(sku)
            cur = td.sizes.get(sku)
            if lv is None or lv.original is None or cur is None or not cur.known:
                continue
            size = line_size(world, sku, tag.base_unit, ln.text)
            gross = gross_line_base(ln.quantity, ln.weight_kg, tag.base_unit == "kg", size.value)
            if gross is None or gross <= EPS:
                continue
            paid = ln.price / gross
            regular = lv.original / cur.value
            tot_base += gross
            tot_units += ln.quantity or 0.0
            if paid <= (1.0 - min_discount) * regular + EPS:
                on_base += gross
                on_units += ln.quantity or 0.0
    if tot_base <= EPS:
        return Assessment("always", 0.0, 0.0, from_prices=False)  # no usable line, e.g. details failed (§6.14)
    return Assessment("on_sale" if on_base > tot_base / 2 + EPS else "always", on_units, tot_units)


def _paid_prices(world: World, tag: TagInfo, td: TagData) -> dict[int, float]:
    """§6.13: a SKU's paid price = Σ line_price_czk ÷ Σ gross base quantity (§6.14) over its window lines with a
    positive price and a positive gross base quantity; a SKU without such a line has none."""
    spend: dict[int, float] = {}
    gross_sum: dict[int, float] = {}
    for ev in td.window:
        if ev.order is None or ev.order.cached is None:
            continue
        for sku, ln in ev.order.cached.lines.items():
            if sku not in tag.skus or ln.price <= 0:
                continue
            size = line_size(world, sku, tag.base_unit, ln.text)
            gross = gross_line_base(ln.quantity, ln.weight_kg, tag.base_unit == "kg", size.value)
            if gross is None or gross <= EPS:
                continue
            spend[sku] = spend.get(sku, 0.0) + ln.price
            gross_sum[sku] = gross_sum.get(sku, 0.0) + gross
    return {s: spend[s] / gross_sum[s] for s in sorted(spend)}


def guess_preferred(world: World, tag: TagInfo, td: TagData, mode: str | None) -> Guess | None:
    """§6.13 preferred-product guess (first run only). Eligible: a `cheapest` tag the agent set, without an `on_sale`
    mode (stored or assessed in this run), with n >= 3 and at least 2 SKUs with a purchase. Its favourite becomes
    the preferred product when its share is above 40 % and its paid price is above the mean paid price of the tag's
    other SKUs that have one."""
    if tag.policy != "cheapest" or tag.source != "agent" or mode == "on_sale" or td.n < GATE_ADD:
        return None
    purchases = [Purchase(ev.date, ev.placement,
                          frozenset(s for s, b in ev.base.items() if b is not None and b > EPS))
                 for ev in td.window if ev.q is not None and ev.q > EPS]
    counts = purchase_counts(purchases)
    if len(counts) < SOLO_GUESS_MIN_SKUS:
        return None
    fav = favourite(purchases)
    total = sum(counts.values())
    if counts[fav] / total <= SOLO_GUESS_SHARE + EPS:
        return None
    paid = _paid_prices(world, tag, td)
    others = [p for s, p in paid.items() if s != fav]
    if fav not in paid or not others:
        return None
    mean = sum(others) / len(others)
    if paid[fav] <= mean + EPS:
        return None
    return Guess(fav, counts[fav], total, paid[fav], mean)


def _variety_mix(world: World, td: TagData, replay: ReplayResult) -> tuple[list[int], dict[int, float], bool]:
    """Mix types (bought in the window, not dropped), weights w_s, and whether every mix type is dropped."""
    tag = td.tag
    bought = sorted({s for ev in td.window for s, u in ev.units.items() if (u or 0) > EPS})
    types = [s for s in bought if (tag.tag_id, s) not in replay.dropped]
    all_dropped = bool(bought) and not types
    pos = [ev for ev in td.history if ev.q is not None and ev.q > EPS]
    basis = [ev for ev in pos if ev.choice][-VARIETY_CHOICE_EVENTS:] or pos[-VARIETY_CHOICE_EVENTS:]
    weights = {}
    for s in types:
        w = sum((ev.base.get(s) or 0.0) for ev in basis)
        size = td.sizes[s].value or 0.0
        weights[s] = w + VARIETY_FLOOR * size
    return types, weights, all_dropped


def _rotation(world: World, td: TagData, state: CycleState, types: list[int], own: Ownership) -> dict[int, float]:
    """Q_s before this run: last 6 placed orders where the tag was bought or offered, plus user-owned cart."""
    tag = td.tag
    orders = []
    for p in reversed(state.placed):
        if not p.active:
            continue
        bought = any(p.has_line(s) for s in tag.skus)
        offered = p.cycle is not None and any(p.cycle.offered(s) > EPS for s in tag.skus)
        if bought or offered:
            orders.append(p)
        if len(orders) >= VARIETY_ROTATION_ORDERS:
            break
    events = {ev.order_id: ev for ev in td.history + td.incoming if ev.order is not None}
    q = {s: 0.0 for s in types}
    for p in orders:
        ev = events.get(p.order_id)
        for s in types:
            got = (ev.base.get(s) or 0.0) if ev else 0.0
            off = 0.0
            if p.cycle is not None and p.cycle.offered(s) > EPS:
                row = p.cycle.owner_row(s)
                if row is not None and row.size is not None and row.size_unit == tag.base_unit:
                    off = p.cycle.offered(s) * row.size
            q[s] += max(got, off)
    for s in types:
        units = own.user_owned.get(s, 0.0)
        size = td.sizes[s].value
        if units > EPS and size:
            q[s] += units * size
    return q


def _candidates(inp: PlanInput, td: TagData, bought: set[int], last_placed: dict[int, datetime]) -> dict[int, Cand]:
    """§6.13 Candidates(g): bought at least once, or the tag's preferred product; not ignored; a known current size;
    live data from this run; orderable; a price."""
    out = {}
    preferred = td.tag.preferred_sku if td.tag.policy == "solo" else None
    for s in td.tag.skus:
        info = inp.world.skus[s]
        lv = inp.live.get(s)
        size = td.sizes[s]
        if s not in bought and s != preferred:
            continue
        if info.ignored or not size.known or lv is None or not lv.orderable(inp.t):
            continue
        price = lv.unit_price
        if price is None:
            continue
        limits = [x for x in (lv.max_available, inp.cart_limits.get(s)) if x is not None]
        room = (min(limits) - inp.own.user_owned.get(s, 0.0)) if limits else None
        out[s] = Cand(s, size.value, price, price / size.value, lv.discount or 0.0, lv.qualifies, room,
                      last_placed.get(s), lv)
    return out


def _line_limit(kind: str, td: TagData) -> float | None:
    """What a line may take (§6.13 fit): P90 for a short-life line, max(need, usual batch) for `always`,
    max(need, MAX) for `on_sale`."""
    need = td.projection.need if td.projection else 0.0
    if kind == "short":
        return td.p90
    if kind == "always":
        return max(need, td.usual_batch or 0.0)
    return max(need, td.max_level or 0.0)


def _fit(cands: dict[int, Cand], limit: float | None) -> dict[int, Cand]:
    """§6.13: the amount comes before the policy. A candidate whose single pack is more than 1.5 × what the line
    may take is dropped while another fits; when none fits, only the smallest pack stays."""
    if not cands or limit is None or limit <= EPS:
        return cands
    fitting = {s: c for s, c in cands.items() if c.size <= FIT_FACTOR * limit + EPS}
    if fitting:
        return fitting
    smallest = min(c.size for c in cands.values())
    return {s: c for s, c in cands.items() if c.size <= smallest + EPS}


def _most_recent(skus: list[int], last_placed: dict[int, datetime]) -> int | None:
    dated = [s for s in skus if s in last_placed]
    if not dated:
        return skus[0] if skus else None
    return max(dated, key=lambda s: (last_placed[s], -s))


def plan_tag(inp: PlanInput, td: TagData, locked: set[str], assessed: dict[str, Assessment], bought: set[int],
             last_placed: dict[int, datetime], h_day: float, kept: set[str] = frozenset()) -> TagPlan:
    tag = td.tag
    tp = TagPlan(tag, td)
    if tag.never:
        tp.category = "never"
        return tp
    mix_types, weights, all_dropped = ([], {}, False)
    if tag.policy == "variety":
        mix_types, weights, all_dropped = _variety_mix(inp.world, td, inp.replay)
    if tag.tag_id in inp.replay.paused or all_dropped:
        tp.category = "paused"
        return tp
    if tag.tag_id in locked:
        tp.category = "locked"
        return tp
    if tag.tag_id in kept:  # §6.11 rule 3: unable to check isn't the same as no longer wanted
        tp.category = "kept"
        return tp
    recent = _most_recent(tag.skus, last_placed)
    if td.size_unknown and (len(td.window) >= GATE_SUGGEST or td.managed):
        return _suggest(tp, "size_unknown", recent)
    due = td.due
    if due and td.n == GATE_SUGGEST and not td.managed:
        return _suggest(tp, "suggest_gate", recent)
    if td.rate is None or (td.n == 0 if td.managed else td.n < GATE_ADD):
        return tp
    mode = tag.stockup_mode
    if tag.long_life and mode is None and tag.tag_id in assessed:
        mode = assessed[tag.tag_id].mode
    tp.kind = "short" if not tag.long_life else ("on_sale" if mode == "on_sale" else "always")
    pre_fit = _candidates(inp, td, bought, last_placed)
    cands = _fit(pre_fit, _line_limit(tp.kind, td))
    tp.cands = cands
    # Policy order and pools (§6.13).
    if tag.policy == "variety":
        alloc_types = [cands[s] for s in mix_types if s in cands]
        ordered = sorted(alloc_types, key=lambda c: (-weights.get(c.sku, 0.0), c.sku))
        breadth = "spread_mix"
        qual = [c for c in ordered if c.qualifies]
    elif tag.policy == "assorted":
        ordered = cheapest_order(list(cands.values()), most_recent_first=False)
        breadth = "spread_equal"
        qual = qualifying_head(ordered)
    else:
        ordered = cheapest_order(list(cands.values()), most_recent_first=True)
        breadth = "single"
        qual = qualifying_head(ordered)
        if tag.policy == "solo":
            # single × preferred: the preferred product first; without it, the cheapest for the need only, and an
            # on_sale tag has no fallback (its sale pool is the preferred product alone)
            pref = tag.preferred_sku
            if pref in cands:
                ordered = [cands[pref]] + [c for c in ordered if c.sku != pref]
                qual = [cands[pref]] if cands[pref].qualifies else []
            else:
                qual = []
                tp.fallback_from = pref
                tp.fallback_too_big = pref in pre_fit
                tp.preferred_issue = ("comes in a bigger pack than needed" if tp.fallback_too_big
                                      else _not_candidate_text(inp, td, pref))
                if tp.kind == "on_sale":
                    ordered = []
    tp.ordered = ordered
    # What the tag wants (§6.11 rules 7-9).
    waiting = tag.tag_id in inp.waiting
    if tp.kind in ("short", "always"):
        wants = "line" if due else None
    elif qual and not waiting:
        wants = "topup"
    elif due and qual and waiting:
        wants = "line"
    elif due:
        wants = "wait_sale"
    else:
        wants = None
    tp.wants = wants
    if wants is None:
        tp.fallback_from = None  # nothing bought, so no fallback (§6.13)
        return _overdue(tp, td, inp.e, recent)
    # Rule 10.
    if not ordered:
        if not any(sz.known for sz in td.sizes.values()):
            tp.fallback_from = None
            return _suggest(tp, "size_unknown", recent)
        # unavailable: the availability text of the most recently bought SKU; for a solo tag, why its preferred
        # product isn't a candidate (§6.13)
        if tp.fallback_from is not None:
            tp.unavailable_reason = tp.preferred_issue
        else:
            tp.unavailable_reason = _availability_text(inp.live.get(recent)) if recent else "no live data"
        return _suggest(tp, "unavailable", recent, category="unavailable")
    if tag.policy == "variety":
        rep = _most_recent([c.sku for c in ordered], last_placed)
    else:
        rep = ordered[0].sku
    if wants == "wait_sale":
        tp.fallback_from = None
        return _suggest(tp, "wait_sale", rep)
    pool = qual if tp.kind == "on_sale" else ordered
    proj = td.projection
    need = proj.need if proj else 0.0
    rotation = _rotation(inp.world, td, inp.state, [c.sku for c in pool], inp.own) if breadth == "spread_mix" else {}
    allocator = Allocator(pool, breadth, weights=weights, rotation=rotation,
                          user_units={s: inp.own.user_owned.get(s, 0.0) for s in tag.skus})
    if tp.fallback_from is not None:
        allocator.fill_normal(need, cap=td.p90 if tp.kind == "short" else None)
        tp.normal_amount = need
    elif tp.kind == "short":
        allocator.fill_normal(need, cap=td.p90)
        tp.normal_amount = min(need, td.p90) if td.p90 is not None else need
    elif tp.kind == "always":
        target = max(need, (td.usual_batch or 0.0) - (proj.incoming_after_h if proj else 0.0))
        allocator.fill_normal(need, toward=target)
        tp.normal_amount = target
    elif wants == "topup":
        allocator.fill_normal(need)
        tp.normal_amount = need
        allocator.fill_stockup(proj.held if proj else 0.0, td.max_level or 0.0)
    else:  # on_sale, due, waiting: one cycle in the qualifying pool
        allocator.fill_normal(need)
        tp.normal_amount = need
    alloc = allocator.alloc
    tp.alloc = alloc
    tp.pool = pool
    if alloc.total_units == 0:
        if alloc.capped_basket:
            return _suggest(tp, "basket_limit", rep)
        tp.fallback_from = None
        if wants == "topup" and not due:  # it wants nothing when the top-up comes to zero units
            tp.wants = None
            return _overdue(tp, td, inp.e, recent)
        return tp
    tp.category = "line"
    tp.reason = "stockup" if (wants == "topup" and not due) else "due"
    tp.saved, tp.extra = topup_value(alloc, cands)
    _rows(tp)
    return tp


def _rows(tp: TagPlan) -> None:
    tp.rows = []
    if tp.alloc is None:
        return
    for sku in tp.alloc.skus():
        c = tp.cands[sku]
        stock = tp.alloc.stockup.get(sku, 0)
        tp.rows.append(PlanRow(tp.tag.tag_id, sku, tp.reason, tp.alloc.normal.get(sku, 0), stock,
                               c.live.sale_until if stock > 0 else None, c.size, tp.tag.base_unit, tp.fallback_from))


def _suggest(tp: TagPlan, reason: str, sku: int | None, category: str = "suggestion") -> TagPlan:
    """A suggestion row. Only an `unavailable` row keeps a solo tag's `fallback_from` (unavailable for want of its
    preferred product); the other suggestions buy nothing, so they aren't a fallback (§6.13)."""
    tp.category = category
    tp.reason = reason
    tp.rep_sku = sku
    if reason != "unavailable":
        tp.fallback_from = None
    if sku is not None:
        size = tp.td.sizes.get(sku) if tp.td else None
        tp.rows = [PlanRow(tp.tag.tag_id, sku, reason, 0, 0, None, size.value if size and size.known else None,
                           tp.tag.base_unit if size and size.known else None, tp.fallback_from)]
    return tp


def _overdue(tp: TagPlan, td: TagData, e: date, recent: int | None) -> TagPlan:
    if td.managed and td.last_purchase is not None and td.usual_gap:
        since = (e - td.last_purchase).days
        if since > OVERDUE_FACTOR * td.usual_gap + EPS:
            tp.note = f"not bought for {_span(since)} (usually every {_span(td.usual_gap, short=True)})"
            # a reminder dated the day the gap was exceeded: the first day more than twice the gap after it
            tp.reminder_since = td.last_purchase + timedelta(days=math.floor(OVERDUE_FACTOR * td.usual_gap + EPS) + 1)
            return _suggest(tp, "overdue", recent)
    return tp


def _span(days: float, short: bool = False) -> str:
    if days >= 14:
        weeks = round(days / 7)
        return f"{weeks}" if short else f"{weeks} weeks"
    d = round(days)
    return f"{d} days" if not short else f"{d} days"


def _availability_text(lv: Live | None) -> str:
    if lv is None:
        return "no live data"
    return lv.reason or (lv.status or "unavailable").lower()


def _not_candidate_text(inp: PlanInput, td: TagData, sku: int) -> str:
    """Why a SKU (a solo tag's preferred product) isn't a candidate, in words: its availability text when Rohlik
    doesn't offer it, else what is missing (§6.13 candidates)."""
    lv = inp.live.get(sku)
    if lv is None or not lv.has_data:
        return "no live data"
    if not lv.orderable(inp.t):
        return _availability_text(lv)
    if not td.sizes[sku].known:
        return "pack size unknown"
    return "no price" if lv.unit_price is None else _availability_text(lv)


def apply_budget(plans: list[TagPlan], budget: float) -> None:
    """Stock-up parts only: by saved_czk desc; accept while the extra spend fits; trim the first that
    doesn't (last-allocated units first), then stop (§6.14)."""
    tops = [tp for tp in plans if tp.category == "line" and tp.alloc and tp.alloc.stockup_total > 0]
    tops.sort(key=lambda tp: (-round(tp.saved, 6), tp.tag.name, tp.tag.tag_id))
    spent = 0.0
    stop = False
    for tp in tops:
        if not stop and spent + tp.extra <= budget + EPS:
            spent += tp.extra
            continue
        while tp.alloc.stockup_total > 0 and (stop or spent + topup_value(tp.alloc, tp.cands)[1] > budget + EPS):
            tp.alloc.drop_last_stockup()
        tp.budget_trimmed = True
        tp.saved, tp.extra = topup_value(tp.alloc, tp.cands)
        spent += tp.extra
        stop = True
        if tp.alloc.total_units == 0:
            rep = tp.ordered[0].sku if tp.ordered else None
            _suggest(tp, "budget_limit", rep)
            if tp.ordered:
                tp.note = f"on sale −{round((tp.ordered[0].live.discount or 0) * 100)} %"
        else:
            tp.reason = "due" if tp.due else "stockup"
            _rows(tp)


def build_plan(inp: PlanInput, raw_units: bool = False) -> PlanResult:
    world = inp.world
    h_day = day_of(inp.t) + inp.cadence
    events = build_events(world, inp.state, inp.e, inp.live)
    bought = _history_skus(world, inp.e)
    last_placed = _last_placed(inp.state)
    locked = {world.skus[s].tag_id for s in inp.own.locked_skus if s in world.skus and world.skus[s].tag_id}
    # §6.11 rule 3: a tag in which an SKU the agent owns got no live data is left as it is, for this run
    kept = {world.skus[s].tag_id for s, q in inp.own.agent_owned.items()
            if q > EPS and s in world.skus and world.skus[s].tag_id
            and not (inp.live.get(s) is not None and inp.live[s].has_data)} - locked
    cart_skus = {s for s, q in inp.own.agent_owned.items() if q > EPS} | {
        s for s, q in inp.own.user_owned.items() if q > EPS}
    datas: dict[str, TagData] = {}
    for tag in sorted(world.tags.values(), key=lambda t: t.sort_key):
        if not tag.skus:
            continue
        datas[tag.tag_id] = build_tag_data(world, tag, events.get(tag.tag_id, []), inp.e, inp.t, h_day, inp.live,
                                           inp.own.user_owned, inp.now, cart_skus=cart_skus)
    assessed: dict[str, Assessment] = {}
    for tag_id, td in datas.items():
        tag = td.tag
        if tag.long_life and tag.stockup_mode is None and td.n >= GATE_ADD:
            # §6.14: only the first run assesses from prices; later a tag reaching n >= 3 without a mode gets always
            assessed[tag_id] = (assess(world, tag, td, inp.live, inp.min_discount) if inp.first_run
                                else Assessment("always", 0.0, 0.0, from_prices=False))
    guesses: dict[str, Guess] = {}
    if inp.first_run:
        for tag_id, td in datas.items():
            tag = td.tag
            mode = tag.stockup_mode or (assessed[tag_id].mode if tag_id in assessed else None)
            guess = guess_preferred(world, tag, td, mode)
            if guess is not None:
                # §7.1 step 9: the first run plans with its guesses, as with its assessed modes; both are stored
                # when it completes (§5.4)
                guesses[tag_id] = guess
                tag.policy, tag.preferred_sku, tag.preferred_source = "solo", guess.sku, "agent"
    plans = [plan_tag(inp, td, locked, assessed, bought, last_placed, h_day, kept) for td in datas.values()]
    apply_budget(plans, inp.budget)
    rows = sorted((r for tp in plans for r in tp.rows),
                  key=lambda r: (world.tags[r.tag_id].name, r.tag_id, r.sku))
    return PlanResult(plans, rows, assessed, locked, events, h_day, kept, guesses)


def cadence_of(history_dates: list[date], override: float | None) -> float:
    """§6.4: median of the last <= 8 gaps between consecutive distinct delivery dates, clamped to [4, 21];
    the user's override wins; fewer than 2 gaps -> 7."""
    if override is not None:
        return float(override)
    days = sorted(set(history_dates))
    gaps = [(b - a).days for a, b in zip(days, days[1:])][-CADENCE_GAPS:]
    if len(gaps) < 2:
        return CADENCE_DEFAULT
    return max(CADENCE_MIN, min(CADENCE_MAX, median(gaps)))


def targets(world: World, result: PlanResult, own: Ownership) -> dict[int, float]:
    """§6.15 targets: planned lines, and removal of agent-owned lines that are no longer wanted."""
    out: dict[int, float] = {}
    planned = set()
    for r in result.rows:
        if r.reason in LINE_REASONS:
            out[r.sku] = own.user_owned.get(r.sku, 0.0) + r.normal_units + r.stockup_units
            planned.add(r.sku)
    for sku, q in sorted(own.agent_owned.items()):
        if q <= EPS or sku in planned:
            continue
        tag = world.tag_of(sku)
        if tag is not None and (tag.tag_id in result.locked_tags or tag.tag_id in result.kept_tags):
            continue
        out[sku] = own.user_owned.get(sku, 0.0)
    return out
