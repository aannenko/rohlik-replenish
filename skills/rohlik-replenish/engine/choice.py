"""Variant choice (§6.13) and discounts (§6.14): candidates, prices, orders, pools and allocation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .constants import EPS
from .sync import _num
from .timeutil import parse_date_loose


@dataclass
class Live:
    sku: int
    name: str | None
    text: str | None
    original: float | None
    sale: float | None
    sale_until: date | None
    sale_counts: bool
    discount: float | None  # Rohlik's marked discount when the sale counts; None when unknown or no sale
    qualifies: bool
    status: str | None
    max_available: float | None
    expected: date | None
    reason: str | None

    @property
    def unit_price(self) -> float | None:
        """What the customer pays per sale unit: salePrice when its sale counts, else originalPrice."""
        return self.sale if self.sale_counts else self.original

    @property
    def has_data(self) -> bool:
        """§4.4: a product without prices or stock has no live data."""
        return self.status is not None and (self.original is not None or self.sale is not None)

    def orderable(self, t: date) -> bool:
        if self.status == "AVAILABLE":
            return self.max_available is None or self.max_available > 0
        if self.status == "PREORDER":
            return self.expected is not None and self.expected <= t
        return False


def parse_live(sku: int, d: dict, e: date, min_discount: float) -> Live:
    prices = d.get("prices") or {}
    stock = d.get("stock") or {}
    original = _num(prices.get("originalPrice"))
    sale = _num(prices.get("salePrice"))
    until = parse_date_loose(prices.get("saleValidTill"))
    counts = sale is not None and (until is None or until > e)  # a sale ending today no longer counts
    discount = None
    if counts and original:
        discount = 1.0 - sale / original
    qualifies = counts and discount is not None and discount >= min_discount - EPS
    status = stock.get("availabilityStatus") if stock else None
    return Live(sku, d.get("name"), d.get("textualAmount"), original, sale, until if counts else None, counts,
                discount, qualifies, status, _num(stock.get("maxAvailableAmount")) if stock else None,
                parse_date_loose(stock.get("expectedReplenishment")) if stock else None,
                (stock.get("availabilityReason") or stock.get("maxBasketAmountReason")) if stock else None)


@dataclass
class Cand:
    sku: int
    size: float
    unit_price: float
    price: float  # per base unit
    discount: float  # tie-break discount: the marked one when its sale counts, else 0
    qualifies: bool
    room: float | None  # agent units that still fit the cart cap; None = no cap known
    last_bought: datetime | None
    live: Live


def cheapest_order(cands: list[Cand], most_recent_first: bool) -> list[Cand]:
    """Ascending price per base unit; ties to the larger discount, then recency, then the lower SKU id."""
    def key(c: Cand):
        ts = c.last_bought.timestamp() if c.last_bought else float("-inf")
        recency = -ts if most_recent_first else ts
        return (round(c.price, 9), -round(c.discount, 9), recency, c.sku)
    return sorted(cands, key=key)


def qualifying_head(ordered: list[Cand]) -> list[Cand]:
    """`cheapest`/`assorted`: the candidates at the head of the cheapest order whose sales qualify."""
    out = []
    for c in ordered:
        if not c.qualifies:
            break
        out.append(c)
    return out


@dataclass
class Allocation:
    normal: dict[int, int] = field(default_factory=dict)
    stockup: dict[int, int] = field(default_factory=dict)
    sequence: list[tuple[int, str]] = field(default_factory=list)  # (sku, phase) in allocation order
    capped_basket: bool = False
    capped_usual: bool = False

    def units(self, sku: int) -> int:
        return self.normal.get(sku, 0) + self.stockup.get(sku, 0)

    @property
    def total_units(self) -> int:
        return sum(self.normal.values()) + sum(self.stockup.values())

    @property
    def stockup_total(self) -> int:
        return sum(self.stockup.values())

    def skus(self) -> list[int]:
        return sorted(s for s in set(self.normal) | set(self.stockup) if self.units(s) > 0)

    def drop_last_stockup(self) -> int | None:
        for i in range(len(self.sequence) - 1, -1, -1):
            sku, phase = self.sequence[i]
            if phase == "stockup":
                del self.sequence[i]
                self.stockup[sku] -= 1
                if self.stockup[sku] == 0:
                    del self.stockup[sku]
                return sku
        return None


class Allocator:
    """breadth x order (§6.13). `pool` is already in the policy's order."""

    def __init__(self, pool: list[Cand], breadth: str, *, weights: dict[int, float] | None = None,
                 rotation: dict[int, float] | None = None, user_units: dict[int, float] | None = None):
        self.pool = pool
        self.breadth = breadth  # single | spread_equal | spread_mix
        self.alloc = Allocation()
        self.base = 0.0
        self.user_units = user_units or {}
        self.rotation = rotation or {}
        if breadth == "spread_mix":
            raw = {c.sku: max(0.0, (weights or {}).get(c.sku, 0.0)) for c in pool}
            total = sum(raw.values())
            self.w = {s: (v / total if total > EPS else 1.0 / len(raw)) for s, v in raw.items()} if raw else {}
        else:
            self.w = {}
        self._allocated_base: dict[int, float] = {}

    def _has_room(self, c: Cand) -> bool:
        return c.room is None or self.alloc.units(c.sku) < c.room - EPS

    def pick(self) -> Cand | None:
        if not self.pool:
            return None
        if self.breadth == "single":
            first = self.pool[0]
            return first if self._has_room(first) else None
        open_ = [c for c in self.pool if self._has_room(c)]
        if not open_:
            return None
        if self.breadth == "spread_equal":
            return min(open_, key=lambda c: (self.user_units.get(c.sku, 0.0) + self.alloc.units(c.sku),
                                             self.pool.index(c)))
        q_s = {c.sku: self.rotation.get(c.sku, 0.0) + self._allocated_base.get(c.sku, 0.0) for c in self.pool}
        total = sum(q_s.values())

        def score(c: Cand):
            gap = self.w.get(c.sku, 0.0) * (total + c.size) - (q_s[c.sku] + c.size)
            ts = c.last_bought.timestamp() if c.last_bought else float("-inf")
            return (-round(gap, 9), ts, c.sku)  # largest gap; ties to the least recently bought, then lower id
        return min(open_, key=score)

    def add(self, c: Cand, phase: str) -> None:
        bucket = self.alloc.normal if phase == "normal" else self.alloc.stockup
        bucket[c.sku] = bucket.get(c.sku, 0) + 1
        self.alloc.sequence.append((c.sku, phase))
        self.base += c.size
        self._allocated_base[c.sku] = self._allocated_base.get(c.sku, 0.0) + c.size

    def fill_normal(self, need: float, cap: float | None = None, toward: float | None = None) -> None:
        """Units until the need is reached (round up), at most `cap` (the P90 cap, units still round up);
        for `always`, then units while the next one brings the total nearer to `toward` (a tie adds it)."""
        goal = need
        if cap is not None and need > cap + EPS:
            goal = cap
            self.alloc.capped_usual = True
        while self.base < goal - EPS:
            c = self.pick()
            if c is None:
                self.alloc.capped_basket = True
                return
            self.add(c, "normal")
        if toward is None:
            return
        while True:
            c = self.pick()
            if c is None:
                smallest = min((x.size for x in self.pool), default=0.0)
                if self.pool and self.base + smallest / 2 < toward - EPS:
                    self.alloc.capped_basket = True
                return
            if abs(self.base + c.size - toward) <= abs(self.base - toward) + EPS:
                self.add(c, "normal")
            else:
                return

    def fill_stockup(self, held: float, max_level: float) -> None:
        """Units while held + this order is below MAX (round up), never a unit taking this order above MAX."""
        while held + self.base < max_level - EPS:
            c = self.pick()
            if c is None:
                self.alloc.capped_basket = True
                return
            if self.base + c.size > max_level + EPS:
                return  # allocation stops; it never skips to a smaller pack
            self.add(c, "stockup")


def topup_value(alloc: Allocation, cands: dict[int, Cand]) -> tuple[float, float]:
    """(saved_czk, extra_spend_czk) of a line's stock-up units (§6.14)."""
    saved = extra = 0.0
    for sku, units in alloc.stockup.items():
        live = cands[sku].live
        if live.sale is None:
            continue
        extra += units * live.sale
        if live.original is not None:
            saved += units * (live.original - live.sale)
    return saved, extra
