"""Rate (§6.8), stock transitions (§6.9) and the due test (§6.10).

One routine serves history, projection and backtest. Time is in days (date ordinals); an event's
batches arrive at the start of its date and consumption is linear within a day. Each past day d consumes
r_d, the rate evaluated at d from the events dated before d; the days from e on consume today's rate.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime

from .constants import EPS


@dataclass(frozen=True)
class StockEvent:
    day: float  # arrival day number (date ordinal)
    placement: datetime
    order_id: int
    q: float  # base quantity (known, >= 0)
    stockup_base: float  # part of q that is stock-up
    user: bool  # user-initiated: may reset the shelf
    arrival_time: datetime  # slot end or end of delivery date, for later resets

    @property
    def key(self) -> tuple:
        return (self.day, self.placement, self.order_id)


@dataclass
class _Batch:
    day: float
    stockup: bool
    seq: int
    arrival_time: datetime
    qty: float

    @property
    def fifo(self) -> tuple:
        return (self.day, self.stockup, self.seq)


class RateHistory:
    """r_d of §6.8 at any day d, from the history events dated before d (§6.9)."""

    def __init__(self, events: Iterable[StockEvent], window_days: float):
        positive = sorted((ev.day, ev.q) for ev in events if ev.q > EPS)
        self.window = float(window_days)
        self.days = [day for day, _ in positive]
        self.prefix = [0.0]
        for _, q in positive:
            self.prefix.append(self.prefix[-1] + q)

    def interval(self, d: float) -> tuple[float, float] | None:
        """[a, b): the first and last dates of the window's events if they differ, else the two most recent
        distinct history dates, else none."""
        days = self.days
        hist_end = bisect_left(days, d)  # events dated before d
        win_start = bisect_right(days, d - self.window)  # ... and after d - W
        if hist_end - win_start >= 1 and days[win_start] < days[hist_end - 1]:
            return days[win_start], days[hist_end - 1]
        if hist_end == 0:
            return None
        b = days[hist_end - 1]
        i = bisect_left(days, b) - 1
        return (days[i], b) if i >= 0 else None

    def rate(self, d: float) -> float | None:
        interval = self.interval(d)
        if interval is None:
            return None
        a, b = interval
        total = self.prefix[bisect_left(self.days, b)] - self.prefix[bisect_left(self.days, a)]
        return total / (b - a)

    def steps(self, start: float, e_day: float, today: float) -> list[tuple[float, float]]:
        """Consumption rates as (from_day, rate) steps: r_d before e, where a day before the first rate uses that
        first rate, and today's rate from e on. r_d changes only when an event enters the history (the day after
        its date) or leaves the window, so it is evaluated only there."""
        points = {start}
        for x in self.days:
            points.add(x + 1)
            points.add(x + self.window)
        steps: list[tuple[float, float]] = []
        for p in sorted(points):
            if p >= e_day:
                break
            r = self.rate(p) if p >= start else None
            if r is not None and (not steps or r != steps[-1][1]):
                steps.append((p, r))
        if not steps:
            return [(-math.inf, today)]
        steps[0] = (-math.inf, steps[0][1])
        steps.append((e_day, today))
        return steps


@dataclass
class StockSim:
    rate: float
    steps: list[tuple[float, float]] | None = None  # (from_day, rate); None = `rate` on every day
    now_day: float = 0.0
    batches: list[_Batch] = field(default_factory=list)
    _seq: int = 0
    _step: int = 0

    def on_hand(self) -> float:
        return sum(b.qty for b in self.batches)

    def _demand(self, x: float, y: float) -> float:
        """What the household eats in [x, y); x never decreases between calls."""
        steps = self.steps or [(-math.inf, self.rate)]
        k = self._step
        while k + 1 < len(steps) and steps[k + 1][0] <= x:
            k += 1
        total = 0.0
        while True:
            end = steps[k + 1][0] if k + 1 < len(steps) else math.inf
            if y <= end:
                total += steps[k][1] * (y - x)
                break
            total += steps[k][1] * (end - x)
            x = end
            k += 1
        self._step = k
        return total

    def advance(self, to_day: float) -> float:
        """Consume from now_day to to_day; returns what was actually eaten."""
        if to_day <= self.now_day:
            return 0.0
        want = self._demand(self.now_day, to_day)
        eaten = 0.0
        self.batches.sort(key=lambda b: b.fifo)
        for b in self.batches:
            if want <= EPS:
                break
            take = min(b.qty, want)
            b.qty -= take
            want -= take
            eaten += take
        self.batches = [b for b in self.batches if b.qty > EPS]
        self.now_day = to_day
        return eaten

    def apply(self, ev: StockEvent) -> None:
        if ev.user and ev.q > EPS:
            # The user was running out of what had arrived before he decided to buy.
            self.batches = [b for b in self.batches if b.stockup or not b.arrival_time < ev.placement]
        stock = min(max(ev.stockup_base, 0.0), ev.q)
        normal = ev.q - stock
        if stock > EPS:
            self._add(ev.day, True, ev.arrival_time, stock)
        if normal > EPS:
            self._add(ev.day, False, ev.arrival_time, normal)

    def add_batch(self, day: float, qty: float, arrival_time: datetime, stockup: bool = False) -> None:
        if qty > EPS:
            self._add(day, stockup, arrival_time, qty)

    def _add(self, day, stockup, arrival_time, qty) -> None:
        self._seq += 1
        self.batches.append(_Batch(day, stockup, self._seq, arrival_time, qty))


@dataclass
class Projection:
    on_hand_t: float  # projected stock at T (before the new batch)
    consumed: float  # eaten in [T, H) without a new batch
    shortfall: float
    incoming_after_t: float
    incoming_after_h: float

    @property
    def held(self) -> float:
        return self.on_hand_t + self.incoming_after_t

    @property
    def due(self) -> bool:
        return self.shortfall > EPS

    @property
    def need(self) -> float:
        return self.shortfall if self.due else 0.0


def project(history: list[StockEvent], incoming: list[StockEvent], rate: float, e_day: float, t_day: float,
            h_day: float, extra: list[StockEvent] = (), new_batch: float = 0.0,
            past: RateHistory | None = None) -> Projection:
    """History from the first event to e, then incoming events and `extra` (the user's cart event at T),
    then [T, H). Past days consume r_d from `past` (§6.9; without it, `rate` throughout), the days from e on
    consume `rate`, today's rate. `new_batch` (tests) adds a planned batch at T, after every known decision."""
    hist = sorted(history, key=lambda ev: ev.key)
    steps = past.steps(hist[0].day, e_day, rate) if past is not None and hist else None
    sim = StockSim(rate, steps)
    if hist:
        sim.now_day = hist[0].day
    for ev in hist:
        sim.advance(ev.day)
        sim.apply(ev)
    sim.advance(e_day)
    if not hist:
        sim.now_day = e_day
    future = sorted(list(incoming) + list(extra), key=lambda ev: ev.key)
    future = [StockEvent(max(ev.day, e_day), ev.placement, ev.order_id, ev.q, ev.stockup_base, ev.user,
                         ev.arrival_time) for ev in future]
    future.sort(key=lambda ev: ev.key)
    i = 0
    while i < len(future) and future[i].day <= t_day:
        sim.advance(future[i].day)
        sim.apply(future[i])
        i += 1
    sim.advance(t_day)
    on_hand = sim.on_hand()
    if new_batch > EPS:
        sim.add_batch(t_day, new_batch, _never(future))
    consumed = 0.0
    while i < len(future) and future[i].day < h_day:
        consumed += sim.advance(future[i].day)
        sim.apply(future[i])
        i += 1
    consumed += sim.advance(h_day)
    after_t = sum(ev.q for ev in future if ev.day > t_day)
    after_h = sum(ev.q for ev in future if ev.day > h_day)
    shortfall = rate * (h_day - t_day) - consumed
    return Projection(on_hand, consumed, shortfall, after_t, after_h)


def _never(future: list[StockEvent]) -> datetime:
    """An arrival time later than every known placement (the planned batch is never reset)."""
    from datetime import timedelta, timezone

    latest = max((ev.placement for ev in future), default=datetime(2000, 1, 1, tzinfo=timezone.utc))
    return latest + timedelta(days=36500)


def day_of(d: date) -> float:
    return float(d.toordinal())
