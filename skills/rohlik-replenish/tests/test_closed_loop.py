"""Closed-loop simulation (§13.3): a synthetic household with known consumption, 26 weeks of the user's own
purchases, then 104 weekly cycles through the real engine against the fake Rohlik."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import pytest

from tests.helpers import Scenario, rohlik_time
from engine.timeutil import PRAGUE

START = date(2026, 1, 5)  # a Monday: week -26
HISTORY_WEEKS = 26
WEEKS = 104
EPS = 1e-9


@dataclass
class Tag:
    name: str
    kind: str  # fresh | weighed (counted in kg, eaten by weight) | pieces (weighed, counted and eaten by the piece)
    #            | variety | always | on_sale
    skus: dict[int, float]  # sku -> nominal size (base units)
    unit: str
    storage: str
    policy: str
    rate: float  # base units per day (before behaviour changes)
    price: dict[int, float]
    mode: str | None = None
    mix: dict[int, float] | None = None  # variety: the household's eating mix
    sale_every: int = 0  # weeks between 40 % sales (on_sale / H)
    sale_until_week: int = 10**6
    piece_kg: float = 0.0  # weighed kinds: the nominal weight of one piece; each piece varies ±15 %

    @property
    def weighed(self) -> bool:
        return self.kind in ("weighed", "pieces")


def household(variety_only: bool = False, h_only: bool = False, fractional: bool = False) -> list[Tag]:
    """Consumption is a whole number of units per weekly cycle, like the spec's reference simulation
    ("single-unit" tags); `fractional=True` gives everyday fractional rates (the §13.1 non-decay test). Two weighed
    tags (§13.3): bananas counted in kg and eaten by weight, apples counted in pieces and eaten by the piece."""
    if fractional:
        rates = {"milk": 0.3, "bread": 0.06, "bananas": 0.12, "apples": 0.4, "rice": 0.1, "coffee": 0.03}
    else:
        rates = {"milk": 1.0 / 7, "bread": 0.5 / 7, "bananas": 0.2 / 7, "apples": 3.0 / 7, "rice": 1.0 / 7,
                 "coffee": 0.25 / 7}
    fresh = [
        Tag("milk", "fresh", {101: 1.0}, "l", "chilled", "cheapest", rates["milk"], {101: 25.0}),
        Tag("bread", "fresh", {102: 0.5}, "kg", "bakery", "cheapest", rates["bread"], {102: 40.0}),
        Tag("bananas", "weighed", {103: 0.2}, "kg", "fresh-produce", "cheapest", rates["bananas"], {103: 9.0},
            piece_kg=0.2),
        Tag("apples", "pieces", {104: 1.0}, "pcs", "fresh-produce", "cheapest", rates["apples"], {104: 7.0},
            piece_kg=0.18),
    ]
    cheese = Tag("sliced cheese", "variety", {301: 0.1, 302: 0.1, 303: 0.1}, "kg", "chilled", "variety",
                 0.3 / 7, {301: 30.0, 302: 32.0, 303: 35.0}, mix={301: 3, 302: 2, 303: 1})
    rice = Tag("rice", "always", {201: 1.0}, "kg", "dry", "cheapest", rates["rice"], {201: 50.0}, mode="always")
    coffee = Tag("coffee", "on_sale", {202: 0.25}, "kg", "dry", "cheapest", rates["coffee"], {202: 150.0},
                 mode="on_sale", sale_every=6)
    pasta = Tag("pasta", "always", {203: 0.5}, "kg", "dry", "cheapest", 1.0 / 7, {203: 40.0}, mode="always",
                sale_every=6, sale_until_week=30)
    if variety_only:
        return [fresh[0], cheese]
    if h_only:
        return [fresh[0], pasta]
    return fresh + [cheese, rice, coffee]


def monday(week: int) -> date:
    return START + timedelta(days=7 * (week + HISTORY_WEEKS))


def dt(d: date, hh: int) -> datetime:
    return datetime.combine(d, time(hh), tzinfo=PRAGUE)


@dataclass
class Stats:
    bought: float = 0.0
    consumed: float = 0.0
    shortage: int = 0
    stock_sum: float = 0.0
    days: int = 0
    units: dict[int, int] = field(default_factory=dict)


class Sim:
    def __init__(self, tmp_path, tags: list[Tag], behaviour: str, seed: int = 7, noise: float = 0.0):
        self.tags = tags
        self.b = behaviour
        self.seed = seed
        self.streams: dict[tuple[str, str], random.Random] = {}
        self.noise = noise  # daily consumption varies by up to ± this fraction
        self.s = Scenario(tmp_path)
        self.stock: dict[str, list[list]] = {t.name: [] for t in tags}  # [arrival_day, qty, sku]
        self.stats: dict[tuple[str, int], Stats] = {}
        self.paused_seen: list[tuple[int, list[str]]] = []
        self.offers: list[tuple[int, dict[int, int]]] = []  # week -> variety units offered per type
        self.deleted_offers = 0
        self.purchases: dict[str, list[tuple[int, float]]] = {}
        self.corrections: dict[tuple[int, str], int] = {}
        self.paused_now: set[str] = set()
        self.went_without: dict[str, float] = {}  # base quantity he wanted but had no stock for, since his last check
        self.add_back: dict[str, float] = {}  # this week's add-back per tag, which a trim leaves in place
        self.oid = 100000
        for t in tags:
            for sku, size in t.skus.items():
                text = f"cca {int(round(t.piece_kg * 1000))} g" if t.weighed else _text(size, t.unit)
                self.s.product(sku, f"{t.name} {sku}", text, t.price[sku], weighted=t.weighed)

    def stream(self, tag: str, purpose: str) -> random.Random:
        """One random stream per tag and purpose (§13.3), so adding a tag doesn't change another tag's draws."""
        key = (tag, purpose)
        if key not in self.streams:
            self.streams[key] = random.Random(f"{self.seed}/{tag}/{purpose}")
        return self.streams[key]

    # ------------------------------------------------------------------ consumption
    def rate(self, t: Tag, week: int) -> float:
        r = t.rate
        if self.b == "E" and week >= 52:
            r *= 2
        if self.b == "G" and week >= 52:
            r *= 0.5
        return r

    def block(self, week: int) -> int:
        return week // 26 if week >= 0 else -1

    def consume_day(self, day: date, week: int) -> None:
        """The household eats a delivery only from its delivery date; stock not yet delivered isn't on hand."""
        today = day.toordinal()
        for t in self.tags:
            need = self.rate(t, week)
            if self.noise:
                need *= 1 + self.noise * (2 * self.stream(t.name, "noise").random() - 1)
            st = self.stats.setdefault((t.name, self.block(week)), Stats())
            st.consumed += need
            batches = self.stock[t.name]
            batches.sort(key=lambda b: b[0])
            left = need
            for b in batches:
                if b[0] > today:
                    break
                take = min(b[1], left)
                b[1] -= take
                left -= take
                if left <= EPS:
                    break
            self.stock[t.name] = [b for b in batches if b[1] > EPS]
            if left > EPS:
                st.shortage += 1
                self.went_without[t.name] = self.went_without.get(t.name, 0.0) + left
            st.stock_sum += sum(b[1] for b in self.stock[t.name] if b[0] <= today)
            st.days += 1

    def real_stock(self, t: Tag) -> float:
        """What is on his shelf when he looks at the cart (Monday: every earlier delivery has arrived)."""
        return sum(b[1] for b in self.stock[t.name])

    def left_at_arrival(self, t: Tag, week: int) -> float:
        """What is left on Wednesday, when this week's order arrives, after eating Monday and Tuesday."""
        return max(0.0, self.real_stock(t) - 2 * self.rate(t, week))

    def count(self, week: int, kind: str, n: int = 1) -> None:
        """§13.3 reports the simulated user's corrections per block: raised, trimmed and added lines, and buys of
        paused tags."""
        if n and week >= 0:
            key = (self.block(week), kind)
            self.corrections[key] = self.corrections.get(key, 0) + n

    # ------------------------------------------------------------------ prices
    def set_prices(self, week: int) -> None:
        for t in self.tags:
            for sku in t.skus:
                on = t.sale_every and week < t.sale_until_week and (week % t.sale_every) in (0, 1)
                if on:
                    end = monday(week - (week % t.sale_every) + 1) + timedelta(days=6)
                    self.s.set_price(sku, sale=round(t.price[sku] * 0.6, 2), sale_until=end.isoformat())
                else:
                    self.s.set_price(sku, sale=None, sale_until=None)

    # ------------------------------------------------------------------ orders
    def deliver(self, lines: dict[int, int], week: int, placed: datetime, manual: bool) -> dict[int, float]:
        """Deliver an order on Wednesday into the household's real stock (and, for the user's own orders, into
        the fake's delivered list). Returns the actual weights of weighed lines."""
        wed = monday(week) + timedelta(days=2)
        weights = {}
        for t in self.tags:
            got = 0.0
            for sku, units in lines.items():
                if sku not in t.skus or units <= 0:
                    continue
                if t.weighed:  # every piece weighs its nominal weight ±15 %; Rohlik reports the total
                    rnd = self.stream(t.name, "weight")
                    w = round(sum(t.piece_kg * rnd.uniform(0.85, 1.15) for _ in range(units)), 3)
                    weights[sku] = w
                qty = weights[sku] if t.kind == "weighed" else units * t.skus[sku]  # kg eaten by weight, else units
                got += qty
                self.stock[t.name].append([wed.toordinal(), qty, sku])
                st = self.stats.setdefault((t.name, self.block(week)), Stats())
                st.bought += qty
                st.units[sku] = st.units.get(sku, 0) + units
            if got > EPS:
                self.purchases.setdefault(t.name, []).append((week, got))
        if manual:
            self.oid += 1
            self.s.delivered(wed.isoformat(), lines, placed=placed, order_id=self.oid, weights=weights)
        return weights

    def own_order(self, week: int) -> dict[int, int]:
        """What the user buys himself in the history weeks (and when a paused tag runs out)."""
        lines = {}
        for t in self.tags:
            r = self.rate(t, week)
            short = 7 * r - self.left_at_arrival(t, week)  # Wednesday's order lasts him to the next Wednesday
            if t.kind == "variety":
                pattern = [301, 301, 302] if week % 2 else [301, 302, 303]
                for sku in pattern:
                    lines[sku] = lines.get(sku, 0) + 1
            elif t.kind == "on_sale":
                sale = t.sale_every and (week % t.sale_every) in (0, 1)
                size = next(iter(t.skus.values()))
                stock_up = r * 7 * (t.sale_every + 1) - self.real_stock(t)  # on sale: through the next sale
                if sale and stock_up > size / 2:
                    lines[next(iter(t.skus))] = math.ceil(stock_up / size)
                elif short > 0:
                    lines[next(iter(t.skus))] = max(1, math.ceil(short / size))
            elif t.kind == "always":
                if short > 0:
                    size = next(iter(t.skus.values()))
                    lines[next(iter(t.skus))] = max(2, math.ceil(short / size))
            elif short > 0:
                size = next(iter(t.skus.values()))
                lines[next(iter(t.skus))] = math.ceil(short / size)
        return lines

    # ------------------------------------------------------------------ the loop
    def history(self) -> None:
        for week in range(-HISTORY_WEEKS, 0):
            mon = monday(week)
            self.went_without.clear()  # his own order is his check of the shelf
            lines = self.own_order(week)
            self.deliver(lines, week, dt(mon, 9), manual=True)
            for i in range(7):
                self.consume_day(mon + timedelta(days=i), week)

    def mapping(self) -> dict:
        m = {}
        for t in self.tags:
            for sku in t.skus:
                extra = {"stockup_mode": t.mode} if t.mode else {}
                m[sku] = (t.name, t.policy, t.unit, t.storage, extra)
        return m

    def run(self) -> "Sim":
        self.history()
        for week in range(0, WEEKS):
            mon = monday(week)
            now = dt(mon, 9)
            wed = mon + timedelta(days=2)
            self.set_prices(week)
            if week == 0:
                self.s.first_runs(now, self.mapping(), date_=wed.isoformat())
                now = now + timedelta(minutes=5)
            env = self.s.run(now, date_=wed.isoformat())
            assert env.status == "ok", env.summary_md
            self.paused_seen.append((week, env.data["paused"]))
            self.user_acts(week, env)
            cart = self.s.cart()
            if cart:
                oid = self.s.checkout(now + timedelta(hours=1), wed.isoformat())
                self.s.run(now + timedelta(hours=3), date_=wed.isoformat(), dry=True)  # sees it as upcoming
                weights = self.deliver(cart, week, now + timedelta(hours=1), manual=False)
                self.s.deliver(oid, weights=weights)
            for i in range(7):
                self.consume_day(mon + timedelta(days=i), week)
        return self

    def user_acts(self, week: int, env) -> None:
        b = self.b
        rows = env.data["plan"]
        agent_skus = {r["sku"] for r in rows if r["reason"] in ("due", "stockup")}
        variety = next((t for t in self.tags if t.kind == "variety"), None)
        if variety:
            offered = {r["sku"]: r["normal_units"] + r["stockup_units"] for r in rows if r["sku"] in variety.skus
                       and r["reason"] in ("due", "stockup")}
            self.offers.append((week, offered))
        if b == "V-G" and variety:
            if 303 in agent_skus:
                self.s.fake.set_cart_qty(303, 0)
                self.deleted_offers += 1
                self.count(week, "trimmed")
            self.raise_to_need(variety, week, exclude={303})
            self.trim_to_bound(variety, week, exclude={303})
        if b == "V-C" and week % 2 and variety:
            lines = [s for s in sorted(variety.skus) if self.s.cart().get(s)]
            if lines:
                pick = self.stream(variety.name, "pick").choice(lines)
                self.s.fake.set_cart_qty(pick, self.s.cart()[pick] + 1)
                self.count(week, "raised")
        if b in ("C", "D", "E", "G"):
            self.paused_now = set(env.data["paused"])
            for t in self.tags:
                if b == "G" and self.real_stock(t) >= 14 * self.rate(t, week):
                    deleted = [s for s in t.skus if s in agent_skus and self.s.cart().get(s)]
                    for s in deleted:
                        self.s.fake.set_cart_qty(s, 0)
                    self.count(week, "trimmed", len(deleted))
                self.raise_to_need(t, week)  # also how he buys a paused tag himself when it runs out (G)
                self.trim_to_bound(t, week)
                if b == "D" and week % 5 == 0:
                    sku = min(t.skus)
                    self.count(week, "raised" if self.s.cart().get(sku) else "added")
                    self.s.fake.set_cart_qty(sku, self.s.cart().get(sku, 0) + 1)

    def raise_to_need(self, t: Tag, week: int, exclude: frozenset | set = frozenset()) -> None:
        """§13.3 C: when his real stock won't last to the next delivery, he raises the line, or adds one, to what he
        needs until then: what is left when the order arrives plus the order must last 7 days. Having gone without
        since his last cart check is itself such a case, and he then also adds back what he went without, so the
        order counts it (§6.8). The build reads this as one target, need + what he went without: a line that already
        covers his need adds back only what its surplus doesn't cover, and needs no raise when the surplus covers it
        all. For a variety slot, across its other lines, one unit at a time."""
        cart = self.s.cart()
        incoming = sum(cart.get(s, 0) * z for s, z in t.skus.items())
        self.add_back[t.name] = self.went_without.pop(t.name, 0.0)  # since his last cart check: this one
        missing = 7 * self.rate(t, week) - self.left_at_arrival(t, week) - incoming + self.add_back[t.name]
        if missing <= EPS:
            return
        skus = [s for s in sorted(t.skus) if s not in exclude]
        lines = [s for s in skus if cart.get(s)] or skus[:1]
        before = {s: cart.get(s, 0) for s in lines}
        while missing > EPS:
            pick = min(lines, key=lambda s: (cart.get(s, 0), s))
            cart[pick] = cart.get(pick, 0) + 1
            missing -= t.skus[pick]
            self.s.fake.set_cart_qty(pick, cart[pick])
        if t.name in self.paused_now:
            self.count(week, "paused_buys")
        else:
            self.count(week, "raised", sum(1 for s in lines if before[s] and cart[s] > before[s]))
            self.count(week, "added", sum(1 for s in lines if not before[s]))

    def stock_bound(self, t: Tag, week: int) -> float:
        """The §13.3 stock bound in base units, as the user knows it: from his consumption and his purchases of
        the last 12 months."""
        cycle = 7 * self.rate(t, week)
        if t.kind == "always":
            return _purchase_level(self, t, week - 52, week, "usual") + 1.5 * cycle
        if t.kind == "on_sale":
            return _purchase_level(self, t, week - 52, week, "max") + cycle
        return max(1.5 * cycle, max(t.skus.values()) + 0.5 * cycle)

    def trim_to_bound(self, t: Tag, week: int, exclude: frozenset | set = frozenset()) -> None:
        """Whenever the agent's line would leave him above the stock bound when it arrives (on Wednesday), he trims
        it to fit, a unit at a time from the largest line, but never below what his raise rule wants: what he needs
        until the next delivery plus this week's add-back."""
        cart = self.s.cart()
        r, left, bound = self.rate(t, week), self.left_at_arrival(t, week), self.stock_bound(t, week)
        floor = 7 * r + self.add_back.get(t.name, 0.0)
        held = sum(cart.get(s, 0) * z for s, z in t.skus.items())
        lines = [s for s in sorted(t.skus) if s not in exclude and cart.get(s)]
        trimmed = set()
        while lines and left + held > bound + EPS:
            pick = max(lines, key=lambda s: (cart[s], s))
            if left + held - t.skus[pick] < floor - EPS:
                break
            cart[pick] -= 1
            held -= t.skus[pick]
            trimmed.add(pick)
            self.s.fake.set_cart_qty(pick, cart[pick])
            if not cart[pick]:
                lines.remove(pick)
        self.count(week, "trimmed", len(trimmed))

    # ------------------------------------------------------------------ results
    def result(self, tag: str, blk: int) -> Stats:
        return self.stats.get((tag, blk), Stats())


def _text(size: float, unit: str) -> str:
    if unit == "kg":
        return f"{int(round(size * 1000))} g"
    if unit == "l":
        return f"{size:g} l".replace(".", ",")
    return f"{size:g} ks"


# ---------------------------------------------------------------------------------------------- assertions
def _purchase_level(sim: Sim, t: Tag, start: int, end: int, kind) -> float:
    """The usual batch (average purchase) or MAX (largest) over the purchases of weeks [start, end)."""
    qs = [q for w, q in sim.purchases.get(t.name, []) if start <= w < end]
    if not qs:
        return 0.0
    return sum(qs) / len(qs) if kind == "usual" else max(qs)


def _level(sim: Sim, t: Tag, blk: int, kind) -> float:
    """The usual batch or MAX over the 12 months before the block ends."""
    end = 26 * (blk + 1)
    return _purchase_level(sim, t, end - 52, end, kind)


def _shares(sim: Sim, blk: int) -> dict[int, float]:
    units = sim.result("sliced cheese", blk).units
    total = sum(units.values())
    return {s: units.get(s, 0) / total for s in (301, 302, 303)} if total else {}


def evaluate(sim: Sim, behaviour: str, only: set[str] | None = None) -> dict[tuple[str, str], str]:
    """Every §13.3 assertion for this behaviour; returns the failing (tag, check) pairs with details. The weighed
    tag counted in kg is exempt from the shortage cap (the user's choice): its shortage days are reported by
    `reported`, while its units bought, and its stock where the user trims, are still asserted."""
    fails: dict[tuple[str, str], str] = {}
    change = behaviour in ("E", "G")
    shortage_only = behaviour == "D"  # impulse extras grow stock until the user trims it
    trims = behaviour in ("C", "E", "G", "V-G")  # the stock bound holds only where the user trims
    for t in sim.tags:
        if only and t.name not in only:
            continue
        if behaviour in ("D", "E") and t.kind == "on_sale":
            continue  # the on_sale tag is asserted in A, C and G (in E its MAX rises only when the user raises it)
        long_life = t.kind in ("always", "on_sale")
        ratio_blocks = [1, 3] if change else [1, 2, 3]  # E/G: from the second block after the change
        caps = {1: 3, 2: 3, 3: 3}
        if behaviour == "E":
            caps = {1: 3, 2: 10, 3: 10 if long_life else 3}
        if t.kind == "weighed":
            caps = {}  # counted in kg: no margin, so its weeks end within grams of empty; reported, not asserted
        for blk, cap in caps.items():
            st = sim.result(t.name, blk)
            if st.shortage > cap:
                fails[(t.name, "shortage")] = f"block {blk}: {st.shortage} shortage days > {cap}"
        if shortage_only:
            continue
        for blk in ratio_blocks:
            st = sim.result(t.name, blk)
            ratio = st.bought / st.consumed if st.consumed else 1.0
            if not 0.8 <= ratio <= 1.3:
                fails[(t.name, "ratio")] = f"block {blk}: bought/consumed {ratio:.2f}"
        if not trims:
            continue
        for blk in ratio_blocks:  # E/G: from the second block after the change, like the units bought
            st = sim.result(t.name, blk)
            avg, cycle = st.stock_sum / st.days, 7 * st.consumed / st.days
            if t.kind == "on_sale":
                limit = cycle + _level(sim, t, blk, "max")
            elif t.kind == "always":
                limit = 1.5 * cycle + _level(sim, t, blk, "usual")
            else:
                limit = max(1.5 * cycle, max(t.skus.values()) + 0.5 * cycle)
            if avg > limit + 1e-6:
                fails[(t.name, "stock")] = f"block {blk}: average stock {avg / cycle:.1f} cycles > {limit / cycle:.1f}"
    if behaviour == "A" and any(p for _, p in sim.paused_seen):
        fails[("*", "paused")] = "a tag was paused in A"
    if behaviour == "V":
        for blk in (1, 2, 3):
            shares = _shares(sim, blk)
            for sku, w in {301: 3 / 6, 302: 2 / 6, 303: 1 / 6}.items():
                if shares.get(sku, 0) <= 0 or abs(shares.get(sku, 0) - w) > 0.10:
                    fails[("sliced cheese", "mix")] = f"block {blk} type {sku}: {shares.get(sku, 0):.2f} vs {w:.2f}"
    if behaviour == "V-C":
        cheese = next(t for t in sim.tags if t.kind == "variety")
        total = sum(cheese.mix.values())
        eligible = sorted(s for s, w in cheese.mix.items() if w / total >= 0.10)  # from the starting mix
        for blk in (1, 2, 3):
            for sku in eligible:
                if not sim.result("sliced cheese", blk).units.get(sku):
                    fails[("sliced cheese", "mix")] = f"block {blk}: type {sku} not bought"
    if behaviour == "V-G":
        if any("sliced cheese" in p for _, p in sim.paused_seen):
            fails[("sliced cheese", "paused")] = "paused"
        if sim.deleted_offers > 2:
            fails[("sliced cheese", "dropped")] = f"the deleted type was offered {sim.deleted_offers} times"
    return fails


def reported(sim: Sim) -> dict:
    """§13.3 "Reported, not asserted", per block 0-3 (block 0, the first 26 engine weeks, isn't asserted either):
    the simulated user's corrections, and the shortage days of the weighed tag counted in kg."""
    blocks = range(WEEKS // 26)
    return {"corrections": {f"block {blk} {kind}": n for (blk, kind), n in sorted(sim.corrections.items())},
            "kg_weighed_shortage_days": {t.name: [sim.result(t.name, blk).shortage for blk in blocks]
                                         for t in sim.tags if t.kind == "weighed"}}


# Spec-level deviations found by this simulation. The test is
# strict both ways: an unlisted failure fails it, and so does a listed deviation that stops failing. The ledger is
# empty. A weighed tag counted in kg is exempt from the shortage cap (the user's choice): with no margin, its run-out
# days depend on the random draws, so `_check` reports them instead (§13.3).
KNOWN: dict[str, dict[tuple[str, str], str]] = {b: {} for b in ("A", "C", "D", "E", "G", "H", "V", "V-C", "V-G")}


def _check(sim: Sim, behaviour: str, only: set[str] | None = None) -> None:
    rep = reported(sim)  # pytest -rP shows these
    print(f"{behaviour} corrections (reported, not asserted): {rep['corrections'] or 'none'}")
    for name, days in rep["kg_weighed_shortage_days"].items():
        print(f"{behaviour} {name}, counted in kg: shortage days per block 0-3 (reported, not asserted): "
              f"{'/'.join(str(n) for n in days)}")
    fails = evaluate(sim, behaviour, only)
    known = KNOWN[behaviour]
    unexpected = {k: v for k, v in fails.items() if k not in known}
    fixed = sorted(k for k in known if k not in fails)
    assert not unexpected, f"{behaviour}: unexpected failures {unexpected}"
    assert not fixed, f"{behaviour}: listed deviations now pass, update KNOWN: {fixed}"


@pytest.mark.parametrize("behaviour", ["A", "C", "D", "E", "G"])
def test_closed_loop_household(tmp_path, behaviour):
    _check(Sim(tmp_path, household(), behaviour).run(), behaviour)


def test_closed_loop_always_tag_ignores_sales(tmp_path):
    """H: an always tag eaten at 2 units a week, with sales that stop at week 30."""
    sim = Sim(tmp_path, household(h_only=True), "A").run()
    _check(sim, "H", {"pasta"})
    assert all(r["stockup_units"] == 0 for env in sim.s.envelopes if env.data
               for r in env.data.get("plan", []) if r["tag"] == "pasta"), "an always tag never tops up"


@pytest.mark.parametrize("behaviour", ["V", "V-C", "V-G"])
def test_closed_loop_variety(tmp_path, behaviour):
    sim = Sim(tmp_path, household(variety_only=True), behaviour).run()
    _check(sim, behaviour, {"sliced cheese"})


def test_harness_eats_a_delivery_only_from_its_delivery_date(tmp_path):
    """Wednesday's delivery can't feed Monday; stock not yet delivered isn't on hand."""
    sim = Sim(tmp_path, household(variety_only=True), "A")
    milk = sim.tags[0]
    wed = monday(0) + timedelta(days=2)
    sim.stock[milk.name].append([wed.toordinal(), 1.0, 101])
    sim.consume_day(monday(0), 0)
    st = sim.result(milk.name, 0)
    assert st.shortage == 1 and st.stock_sum == 0.0
    assert sim.stock[milk.name][0][1] == 1.0
    sim.consume_day(wed, 0)
    assert sim.stock[milk.name][0][1] == pytest.approx(1.0 - milk.rate)


def test_v_c_types_come_from_the_starting_mix(tmp_path):
    """A type with ≥ 10 % of the starting mix must be bought in every block, even when it vanished."""
    sim = Sim(tmp_path, household(variety_only=True), "V-C")
    for blk in (1, 2, 3):
        st = sim.stats.setdefault(("sliced cheese", blk), Stats())
        st.bought = st.consumed = 7.8
        st.days, st.stock_sum, st.units = 182, 182 * 0.3, {301: 78}
        sim.stats[("milk", blk)] = Stats(bought=26.0, consumed=26.0, days=182, stock_sum=182.0)
    fails = evaluate(sim, "V-C", {"sliced cheese"})
    assert fails.get(("sliced cheese", "mix")) == "block 3: type 303 not bought"


def test_closed_loop_fractional_noisy_consumption_does_not_decay(tmp_path):
    """§13.1/§6.9: under A, with fractional rates (milk 2.1 l, bananas ~4 pieces, apples 2.8, bread 0.84 loaves a
    week) and consumption varying ±30 % day to day, every tag is still bought at least 80 % of what is eaten in every
    block. Replaying the past at today's rate decayed milk, bananas, bread and rice to nothing within two years.
    Shortage days aren't asserted: ±30 % daily noise with no margin always causes some."""
    sim = Sim(tmp_path, household(fractional=True), "A", noise=0.3).run()
    for t in sim.tags:
        for blk in (1, 2, 3):
            st = sim.result(t.name, blk)
            assert st.bought / st.consumed >= 0.8, f"{t.name} block {blk}: bought/consumed {st.bought / st.consumed:.2f}"


def test_random_draws_come_from_one_stream_per_tag_and_purpose(tmp_path):
    """§13.3: adding a tag doesn't change another tag's draws."""
    def banana_weights(tags):
        sim = Sim(tmp_path / str(len(tags)), tags, "A")
        return [sim.deliver({103: 3}, week, dt(monday(week), 9), manual=False)[103] for week in range(5)]

    bananas = next(t for t in household() if t.name == "bananas")
    apples = next(t for t in household() if t.name == "apples")
    alone = banana_weights([bananas])
    assert alone == banana_weights([apples, bananas]) == banana_weights(household())
    assert all(0.51 <= w <= 0.69 for w in alone), "3 pieces of 0.2 kg ± 15 %"


def test_the_correcting_user_adds_back_what_he_went_without(tmp_path):
    """§13.3 C: he raises the line to what he needs until the next delivery, plus what he went without since his
    last cart check, also when the line already covers his need; a trim leaves that add-back in place."""
    sim = Sim(tmp_path, household(), "C")
    apples = next(t for t in sim.tags if t.name == "apples")
    for sku in apples.skus:
        sim.s.fake.set_cart_qty(sku, 3)  # the agent's line: exactly a week's need, 3 pieces
    sim.consume_day(monday(0), 0)  # an empty shelf: 3/7 of a piece missed on Monday
    sim.raise_to_need(apples, 1)
    assert sim.s.cart()[104] == 4, "3 for the week ahead, plus 1 for what he went without"
    assert sim.add_back["apples"] == pytest.approx(3 / 7) and "apples" not in sim.went_without
    sim.raise_to_need(apples, 1)
    assert sim.s.cart()[104] == 4 and sim.add_back["apples"] == 0.0, "counted once, at the next check"


def test_the_add_back_raises_to_need_plus_what_he_went_without(tmp_path):
    """The build's reading of §13.3 C: one target, his need until the next
    delivery plus what he went without. A line whose surplus over his need covers the add-back needs no raise; a
    smaller surplus covers part of it; and a trim never takes him below the target."""
    sim = Sim(tmp_path, household(), "C")
    apples = next(t for t in sim.tags if t.name == "apples")
    r, sunday = apples.rate, monday(1).toordinal() - 1  # 3 pieces a week; Monday's check of week 1

    def check(on_shelf_at_arrival: float, went_without: float, line: int) -> int:
        sim.stock["apples"] = [[sunday, on_shelf_at_arrival + 2 * r, 104]] if on_shelf_at_arrival else []
        sim.went_without["apples"] = went_without
        sim.s.fake.set_cart_qty(104, line)
        sim.raise_to_need(apples, 1)
        sim.trim_to_bound(apples, 1)
        return sim.s.cart().get(104, 0)

    assert check(2.0, 3 / 7, 3) == 2, "a surplus of 2 pieces covers the add-back: no raise, and the trim to the bound"
    assert check(0.2, 3 / 7, 3) == 4, "a surplus of 0.2 covers part of it: 1 more piece"
    assert check(0.0, 2.0, 3) == 5, "above the bound of 4.5 pieces, but a trim never removes the add-back"


def test_the_kg_weighed_tag_is_exempt_from_the_shortage_cap_but_not_from_its_amounts(tmp_path):
    """§13.3: the weighed tag counted in kg has its shortage days reported, not asserted; its units bought,
    and its stock where the user trims, still are. The weighed tag counted in pieces keeps the cap."""
    sim = Sim(tmp_path, household(), "C")
    for t in sim.tags:
        for blk in (1, 2, 3):
            sim.stats[(t.name, blk)] = Stats(bought=10.0, consumed=10.0, days=182)
    for blk in (1, 2, 3):
        sim.stats[("bananas", blk)].shortage = 13
    assert evaluate(sim, "C") == {}, "13 run-out days a half-year of the kg tag fail nothing"
    assert reported(sim)["kg_weighed_shortage_days"] == {"bananas": [0, 13, 13, 13]}
    sim.stats[("apples", 2)].shortage = 4
    assert set(evaluate(sim, "C")) == {("apples", "shortage")}, "the tag counted in pieces keeps the cap"
    sim.stats[("apples", 2)].shortage = 0
    sim.stats[("bananas", 3)].bought = 7.0
    assert set(evaluate(sim, "C")) == {("bananas", "ratio")}, "70 % of consumption bought"
    sim.stats[("bananas", 3)].bought = 10.0
    sim.stats[("bananas", 1)].stock_sum = 182 * 10.0
    assert set(evaluate(sim, "C")) == {("bananas", "stock")}, "the stock bound, where the user trims"
    assert set(evaluate(sim, "A")) == set(), "no stock bound where he never trims"
