"""A synthetic household in the `--fixtures DIR` layout (engine/fake.py), with its `tags.json`: made-up products,
prices and orders, so the tests that drive a whole account through the CLI run on any machine. The recorded
fixtures (a real account's history, de-identified) are private and stay out of version control; the tests that
need them are marked `recorded` (tests/helpers.py).

`uv run --with pytest --with-requirements replenish.requirements.txt python -m tests.synthetic DIR` writes the
directory, e.g. to try the CLI without an account: with `ROHLIK_REPLENISH_DATA_DIR` set to a scratch directory,
`uv run replenish.py run --fixtures DIR --now 2026-10-26T12:00:00+01:00 --date 2026-10-28`, then
`tags apply DIR/tags.json` and the same `run` again."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

from tests.helpers import Scenario

BREAD, MILK_A, MILK_B, EGGS_L, EGGS_M = 101, 102, 103, 104, 105
YOGURT_A, YOGURT_B, YOGURT_C = 111, 112, 113
BANANA, BUNCH, POTATO = 121, 122, 123
PASTA, COFFEE, WATER, PEAS = 131, 132, 133, 134
PAPER, CLEANER = 141, 142
CANDLES, MANGO = 151, 152

# sku -> (product tag, policy, base unit, storage tag[, extra fields]), as an agent would tag them
TAGS = {
    BREAD: ("rye bread", "cheapest", "kg", "bakery"),
    MILK_A: ("milk 1.5%", "cheapest", "l", "chilled"),
    MILK_B: ("milk 1.5%", "cheapest", "l", "chilled"),
    EGGS_L: ("eggs", "solo", "pcs", "chilled", {"preferred": True}),
    EGGS_M: ("eggs", "solo", "pcs", "chilled"),
    YOGURT_A: ("yogurt plain", "cheapest", "kg", "chilled"),
    YOGURT_B: ("yogurt plain", "cheapest", "kg", "chilled"),
    YOGURT_C: ("yogurt plain", "cheapest", "kg", "chilled"),
    BANANA: ("bananas", "cheapest", "pcs", "fresh-produce"),
    BUNCH: ("bananas", "cheapest", "pcs", "fresh-produce", {"units_per_pack": 4}),
    POTATO: ("potatoes", "cheapest", "kg", "fresh-produce"),
    PASTA: ("spaghetti", "cheapest", "kg", "dry"),
    COFFEE: ("coffee beans", "cheapest", "kg", "dry"),
    WATER: ("still water", "cheapest", "l", "drinks"),
    PEAS: ("peas", "cheapest", "kg", "frozen"),
    PAPER: ("toilet paper", "cheapest", "pcs", "drugstore-household"),
    CLEANER: ("dishwasher cleaner", "cheapest", "l", "drugstore-household"),
    CANDLES: ("birthday candles", "cheapest", "pcs", "other"),
    MANGO: ("mango", "cheapest", "pcs", "fresh-produce"),
}


def household(tmp: Path) -> Scenario:
    """34 weekly orders, 5 Mar – 22 Oct 2026. The favourite plain yogurt (5 of the last 10 orders) is the priciest
    of three, so the first run guesses `solo` for it; the milk and the bananas bought more often are the cheaper
    ones, so their tags stay `cheapest`. Bananas come loose and once as a bunch of 4; the dishwasher cleaner's pack
    text changed after its 3 orders (so its paid prices look like a deep sale: `on_sale` is guessed); candles and a
    mango were bought once."""
    s = Scenario(tmp)
    s.product(BREAD, "Rye bread", "500 g", 39.9)
    s.product(MILK_A, "Valley milk 1.5%", "1 l", 22.9)
    s.product(MILK_B, "Farm milk 1.5%", "1 l", 24.9)
    s.product(EGGS_L, "Free-range eggs L", "10 ks", 69.9)
    s.product(EGGS_M, "Barn eggs M", "10 ks", 54.9)
    s.product(YOGURT_A, "Alpine plain yogurt", "150 g", 30.0)
    s.product(YOGURT_B, "Meadow plain yogurt", "150 g", 25.0)
    s.product(YOGURT_C, "Basic plain yogurt", "150 g", 24.0)
    s.product(BANANA, "Banana 1 pc", "cca 190 g", 8.0, weighted=True)
    s.product(BUNCH, "Bananas, bunch", "cca 0,65 kg", 34.0, weighted=True)  # pricier per piece: no solo guess
    s.product(POTATO, "Potato 1 pc", "cca 190 g", 3.0, weighted=True)
    s.product(PASTA, "Spaghetti", "500 g", 34.9)
    s.product(COFFEE, "Coffee beans", "1 kg", 349.0)
    s.product(WATER, "Still water", "6×1,5 l", 59.4)
    s.product(PEAS, "Frozen peas", "450 g", 44.9)
    s.product(PAPER, "Toilet paper", "8 ks", 99.9)
    s.product(CLEANER, "Dishwasher cleaner", "2 l", 89.9)
    s.product(CANDLES, "Birthday candles", "24 ks", 29.9)
    s.product(MANGO, "Mango", "1 ks", 39.9)
    yogurts = [YOGURT_A, YOGURT_B, YOGURT_A, YOGURT_C, YOGURT_A, YOGURT_B, YOGURT_A, YOGURT_C, YOGURT_A, YOGURT_B]
    day = date(2026, 3, 5)
    for i in range(34):
        lines = {BREAD: 1, MILK_B if i % 3 == 0 else MILK_A: 2, BANANA: 5, POTATO: 6}
        weights = {BANANA: 1.0 if i % 2 else 0.9, POTATO: 1.15}
        if i % 2 == 0:
            lines[EGGS_M if i % 8 == 6 else EGGS_L] = 1
        if i >= 24:
            lines[yogurts[i - 24]] = 2
        if i % 3 == 0:
            lines[PASTA] = 2
        if i % 4 == 1:
            lines[COFFEE] = 1
        if i % 2 == 1:
            lines[WATER] = 1
        if i % 4 == 3:
            lines[PEAS] = 1
        if i % 5 == 2:
            lines[PAPER] = 1
        if i in (5, 15, 25):
            lines[CLEANER] = 1
        if i == 10:
            lines[CANDLES] = 1
        if i == 20:
            lines[BUNCH] = 1
            weights[BUNCH] = 0.71
        if i == 30:
            lines[MANGO] = 1
        s.delivered(day.isoformat(), lines, weights=weights)
        day += timedelta(days=7)
    s.fake.products[CLEANER]["textualAmount"] = "500 ml"  # the pack changed: '2 l' in 3 orders, '500 ml' now
    s.fake.cancelled.append({"id": 4999, "orderTime": "2026-02-20T09:15:00.000+0100", "itemsCount": 7,
                             "state": "CANCELLED"})
    return s


def tag_entries() -> list[dict]:
    entries = []
    for sku, spec in TAGS.items():
        entry = {"sku": sku, "product_tag": spec[0], "policy": spec[1], "base_unit": spec[2], "storage_tag": spec[3]}
        if len(spec) > 4:
            entry.update(spec[4])
        entries.append(entry)
    return entries


def write(out: Path) -> Path:
    """Writes the household's fixture directory, with its `tags.json`, to `out` and returns `out`."""
    fake = household(out).fake
    files = {
        "user_info.json": fake.user_info,
        "delivered.json": fake.delivered,
        "upcoming.json": fake.upcoming,
        "cancelled.json": fake.cancelled,
        "cart.json": {"items": {}, **fake.cart_info},
        "products.json": {str(sku): {k: v for k, v in p.items() if not k.startswith("_")}
                          for sku, p in sorted(fake.products.items())},
        "checkout_link.json": fake.checkout_link,
        "tags.json": tag_entries(),
    }
    out.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    print(write(Path(sys.argv[1])))
