"""Record read-only Rohlik responses as anonymised test fixtures (spec §13.7).

Usage (uses the locked dependency versions; log in first with `uv run replenish.py login`):

    uv run --with-requirements replenish.requirements.txt python tools/record_fixtures.py OUT_DIR [--timeslots]

Reads only: get_user_info, fetch_orders (delivered for the last 12 months, upcoming, cancelled), get_cart,
get_product_details for every product seen, get_checkout_link, and with --timeslots get_timeslots_checkout
days 0-3. Writes the directory layout `--fixtures DIR` serves (see engine/fake.py).

Anonymisation: names, e-mails, addresses, phones, courier, payment and delivery-note fields are dropped; the
account ID and order IDs are replaced by stable synthetic ones. Product data (public catalogue) is kept.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.db import connect  # noqa: E402
from engine.paths import AuthFile, data_dir  # noqa: E402
from engine.rohlik import RohlikClient  # noqa: E402
from engine.sync import read_delivered  # noqa: E402
from engine.timeutil import local_date, utcnow  # noqa: E402

SYNTHETIC_ACCOUNT = 1000001
ORDER_ID_BASE = 900000001

ORDER_KEEP = {"id", "itemsCount", "orderTime", "deliverySlot", "state", "priceComposition", "items",
              "totalPrice", "warrantyInfo", "deliveryAnnouncements"}
PRICE_KEEP = {"goods", "total", "creditsUsed", "delivery"}  # courier tips and payment fees are dropped
ITEM_KEEP = {"id", "name", "unit", "textualAmount", "quantity", "amount", "totalPrice", "currency", "price"}
WARRANTY_KEEP = {"productId", "name", "pieces", "alreadyClaimed", "alreadyClaimedPieces", "remainingPieces",
                 "weighted", "totalWeight", "pricePerPiece", "total"}
CART_KEEP = {"totalPrice", "totalSavings", "minimalStandardOrderPrice", "minimalDeliveryPointOrderPrice",
             "minimalOrderPrice", "submitConditionPassed", "items"}
CART_LINE_KEEP = {"productId", "productName", "quantity", "textualAmount", "maxBasketAmount", "maxBasketAmountReason",
                  "price", "totalPrice", "unit"}
PRODUCT_DROP = {"description"}


class IdMap:
    def __init__(self):
        self.ids: dict[int, int] = {}

    def __call__(self, original) -> int:
        original = int(original)
        if original not in self.ids:
            self.ids[original] = ORDER_ID_BASE + len(self.ids)
        return self.ids[original]


def clean_order(o: dict, ids: IdMap) -> dict:
    out = {k: v for k, v in o.items() if k in ORDER_KEEP}
    out["id"] = ids(o["id"])
    if isinstance(out.get("deliverySlot"), dict):
        out["deliverySlot"] = {"timeRange": out["deliverySlot"].get("timeRange")}
    if isinstance(out.get("priceComposition"), dict):
        out["priceComposition"] = {k: v for k, v in out["priceComposition"].items() if k in PRICE_KEEP}
    out["items"] = [{k: v for k, v in it.items() if k in ITEM_KEEP} for it in o.get("items") or []]
    w = o.get("warrantyInfo")
    if isinstance(w, dict):
        out["warrantyInfo"] = {key: [{k: v for k, v in e.items() if k in WARRANTY_KEEP} for e in w.get(key) or []]
                               for key in ("enabledData", "disabledData")}
    if isinstance(out.get("deliveryAnnouncements"), list):
        out["deliveryAnnouncements"] = [{"isDelayed": a.get("isDelayed")} for a in out["deliveryAnnouncements"]
                                        if isinstance(a, dict)]
    return out


def clean_cancelled(o: dict, ids: IdMap) -> dict:
    return {"id": ids(o["id"]), "orderTime": o.get("orderTime"), "itemsCount": o.get("itemsCount"),
            "state": o.get("state")}


def clean_cart(data: dict) -> dict:
    out = {k: v for k, v in data.items() if k in CART_KEEP}
    out["items"] = {str(k): {kk: vv for kk, vv in line.items() if kk in CART_LINE_KEEP}
                    for k, line in (data.get("items") or {}).items()}
    return out


def record(out: Path, timeslots: bool) -> dict:
    from engine.mcp_transport import McpTransport, SecretStore, TokenStore

    store = TokenStore(SecretStore(), AuthFile(data_dir()))
    if not store.has_session():
        raise SystemExit("not logged in: run `uv run replenish.py login` first")
    transport = McpTransport(store)
    client = RohlikClient(transport)
    now = utcnow()
    e = local_date(now)
    ids = IdMap()
    try:
        info = client.get_user_info()
        xtra = ((info.get("xtra") or {}).get("limits") or {}).get("orders_without_min_remaining")
        scratch = connect(":memory:", now)  # empty cache: read_delivered pages 12 months forward
        delivered = [clean_order(o, ids) for o in read_delivered(scratch, client, e)]
        upcoming = [clean_order(o, ids) for o in client.fetch_orders("upcoming")]
        cancelled = [clean_cancelled(o, ids) for o in client.fetch_orders("cancelled")]
        cart = clean_cart(client.get_cart())
        skus = {int(it["id"]) for o in delivered + upcoming for it in o["items"]} | {int(k) for k in cart["items"]}
        details = client.product_details(sorted(skus))
        products = {str(k): {kk: vv for kk, vv in v.items() if kk not in PRODUCT_DROP}
                    for k, v in sorted(details.items())}
        link = {"checkout_link": client.get_checkout_link()}
        slots = {}
        if timeslots:
            for day in range(4):
                slots[str(day)] = {"data": client.get_timeslots(day)}
    finally:
        transport.close()
    out.mkdir(parents=True, exist_ok=True)

    def write(name, value):
        (out / name).write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                                encoding="utf-8")
    write("user_info.json", {"id": SYNTHETIC_ACCOUNT, "xtra": {"limits": {"orders_without_min_remaining": xtra}}})
    write("delivered.json", sorted(delivered, key=lambda o: o["id"]))
    write("upcoming.json", upcoming)
    write("cancelled.json", cancelled)
    write("cart.json", cart)
    write("products.json", products)
    write("checkout_link.json", link)
    if slots:
        write("timeslots.json", slots)
    return {"delivered": len(delivered), "upcoming": len(upcoming), "cancelled": len(cancelled),
            "cart_lines": len(cart["items"]), "products": len(products), "recorded_on": e.isoformat()}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("out", type=Path)
    p.add_argument("--timeslots", action="store_true", help="also record get_timeslots_checkout days 0-3")
    args = p.parse_args()
    print(json.dumps(record(args.out, args.timeslots)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
