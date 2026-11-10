"""Scenario builder for tests: a fake Rohlik plus an in-memory database, driven only through the engine's API."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from engine.db import connect
from engine.fake import FakeRohlik
from engine.paths import AuthFile, Config
from engine.rohlik import RohlikClient
from engine.runner import Env, SessionInfo, run
from engine.tagsvc import tags_apply
from engine.timeutil import PRAGUE, parse_iso

# A real account's history, de-identified (tools/record_fixtures.py) and a version-1 database made from it: private,
# kept out of version control. The tests that need them are marked `recorded` and skip without them; the rest run
# on synthetic households (tests/synthetic.py, Scenario).
RECORDED = Path(__file__).parent / "fixtures" / "recorded"
RECORDED_V1 = Path(__file__).parent / "fixtures" / "v1" / "recorded.sql"
recorded = pytest.mark.skipif(not (RECORDED.is_dir() and RECORDED_V1.is_file()),
                              reason="needs the private recorded fixtures (tests/fixtures/recorded and "
                                     "tests/fixtures/v1/recorded.sql), which are not in the repository")


def d(text: str) -> date:
    return date.fromisoformat(text)


def at(day: str, hh: int = 8, mm: int = 0) -> datetime:
    """A local Prague time as an aware datetime."""
    return datetime.combine(date.fromisoformat(day), time(hh, mm), tzinfo=PRAGUE)


def section(md: str, title: str) -> list[str]:
    """The lines of one `### <title>` section of a summary (§8.1); [] when the section is absent."""
    head = f"### {title}\n"
    if head not in md:
        return []
    body = md.split(head, 1)[1]
    return body.split("\n### ", 1)[0].split("\n## ", 1)[0].strip().splitlines()


def rohlik_time(dt: datetime) -> str:
    loc = dt.astimezone(PRAGUE)
    off = loc.utcoffset()
    sign = "+" if off >= timedelta(0) else "-"
    mins = abs(int(off.total_seconds() // 60))
    return loc.strftime("%Y-%m-%dT%H:%M:%S.000") + f"{sign}{mins // 60:02d}{mins % 60:02d}"


class Scenario:
    def __init__(self, tmp_path: Path, *, config: Config | None = None):
        self.tmp = tmp_path
        self.fake = FakeRohlik()
        self.conn = None
        self.auth = AuthFile(tmp_path)
        self.config = config or Config()
        self.session = SessionInfo()
        self.next_order = 5000
        self.envelopes = []

    # ------------------------------------------------------------------ catalogue
    def product(self, sku: int, name: str, text: str, price: float, *, sale: float | None = None,
                original: float | None = "same", sale_until: str | None = None, status: str = "AVAILABLE",
                max_available: float = 50, expected: str | None = None, weighted: bool = False,
                reason: str | None = None, no_stock: bool = False) -> None:
        orig = price if original == "same" else original
        stock = None if no_stock else {"availabilityStatus": status, "maxAvailableAmount": max_available,
                                       "availabilityReason": reason}
        if expected:
            stock["expectedReplenishment"] = expected
        self.fake.products[sku] = {
            "name": name, "brand": None, "textualAmount": text, "countriesOfOrigin": [],
            "prices": {"originalPrice": orig, "salePrice": sale, "unitPrice": None, "saleId": 1 if sale else None,
                       "saleValidTill": sale_until, "saleText": None, "currency": "CZK"},
            "_weighted": weighted}
        if stock is not None:
            self.fake.products[sku]["stock"] = stock

    def set_price(self, sku: int, *, sale=None, sale_until=None, original="keep", status=None):
        p = self.fake.products[sku]["prices"]
        p["salePrice"] = sale
        p["saleValidTill"] = sale_until
        if original != "keep":
            p["originalPrice"] = original
        if status is not None:
            self.fake.products[sku]["stock"]["availabilityStatus"] = status

    # ------------------------------------------------------------------ orders
    def delivered(self, day: str, lines: dict[int, float], *, placed: datetime | None = None, order_id: int | None = None,
                  slot: str = "08:00-09:00", claims: dict | None = None, weights: dict | None = None,
                  prices: dict | None = None) -> int:
        oid = order_id or self._oid()
        placed = placed or at(day, 8) - timedelta(hours=14)
        items, warranty = [], []
        for sku, qty in sorted(lines.items()):
            prod = self.fake.products.get(sku, {})
            unit_price = (prices or {}).get(sku)
            if unit_price is None:
                pr = prod.get("prices", {})
                unit_price = pr.get("salePrice") or pr.get("originalPrice") or 10.0
            items.append({"id": sku, "name": prod.get("name", f"P{sku}"), "quantity": qty, "unit": "kg",
                          "textualAmount": prod.get("textualAmount"), "totalPrice": round(unit_price * qty, 2),
                          "currency": "CZK"})
            warranty.append({"productId": sku, "pieces": qty, "alreadyClaimedPieces": (claims or {}).get(sku, 0),
                             "weighted": bool(prod.get("_weighted")), "totalWeight": (weights or {}).get(sku),
                             "remainingPieces": qty})
        self.fake.delivered.append({
            "id": oid, "orderTime": rohlik_time(placed), "deliverySlot": {"type": "X", "timeRange": f"{day} {slot}"},
            "state": "DELIVERED", "items": items,
            "priceComposition": {"total": {"amount": sum(i["totalPrice"] for i in items), "currency": "CZK"}},
            "warrantyInfo": {"enabledData": warranty, "disabledData": []}})
        return oid

    def _oid(self) -> int:
        self.next_order += 1
        return self.next_order

    def history(self, start: str, every: int, count: int, lines: dict[int, float]) -> list[int]:
        """`count` orders `every` days apart starting at `start`, each with the same lines."""
        out = []
        day = d(start)
        for _ in range(count):
            out.append(self.delivered(day.isoformat(), lines))
            day += timedelta(days=every)
        return out

    def checkout(self, now: datetime, delivery_day: str, *, with_order_time: bool = False, clear_cart: bool = True,
                 order_id: int | None = None) -> int:
        oid = order_id or self._oid()
        self.fake.checkout(oid, rohlik_time(now), f"{delivery_day} 08:00-09:00", clear_cart=clear_cart,
                           with_order_time=with_order_time)
        return oid

    def deliver(self, order_id: int, **kw) -> None:
        self.fake.deliver(order_id, **kw)

    # ------------------------------------------------------------------ engine
    def _db(self, now: datetime):
        if self.conn is None:
            self.conn = connect(":memory:", now)
        return self.conn

    def run(self, now: datetime | str, *, date_: str | None = None, dry: bool = False, clock=None):
        """`clock` (optional) returns the time now during the run, e.g. at the start of the write phase."""
        if isinstance(now, str):
            now = parse_iso(now)
        client = RohlikClient(self.fake, sleep=lambda s: None)
        env = Env(self.tmp, client, now, self.config, self.auth, self.session, lambda account: self._db(now),
                  fixtures=True, clock=clock)
        result = run(env, d(date_) if date_ else None, dry)
        self.envelopes.append(result)
        return result

    def tag(self, entries: list[dict], now: datetime | None = None, source: str = "agent"):
        return tags_apply(self.conn, entries, source, now or datetime(2026, 1, 1, tzinfo=PRAGUE))

    def tag_all(self, mapping: dict[int, tuple], now: datetime | None = None, source: str = "agent"):
        """mapping: sku -> (tag name, policy, base unit, storage tag[, extra dict])."""
        entries = []
        for sku, spec in mapping.items():
            entry = {"sku": sku, "product_tag": spec[0], "policy": spec[1], "base_unit": spec[2],
                     "storage_tag": spec[3]}
            if len(spec) > 4:
                entry.update(spec[4])
            entries.append(entry)
        return self.tag(entries, now, source)

    def first_runs(self, now: datetime, mapping: dict[int, tuple], *, date_: str | None = None):
        """needs_tags -> tag -> first (dry) run."""
        env1 = self.run(now, date_=date_)
        assert env1.status == "needs_tags", env1.summary_md
        self.tag_all(mapping, now)
        env2 = self.run(now, date_=date_)
        assert env2.status == "dry_run", env2.summary_md
        return env2

    def cart(self) -> dict[int, float]:
        return {sku: line["quantity"] for sku, line in sorted(self.fake.cart.items()) if line["quantity"] > 0}

    def rows(self, env) -> dict[int, dict]:
        return {r["sku"]: r for r in env.data.get("plan", [])}

    def dump(self, env) -> str:
        return json.dumps(env.data, indent=1, ensure_ascii=False)
