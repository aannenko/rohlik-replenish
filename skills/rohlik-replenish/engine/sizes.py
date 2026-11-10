"""Pack sizes and base quantities — the input boundary (spec §6.2, grammar §10.4).

Later stages use only base quantities, sizes and `None` for "unknown".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SPACES = re.compile(r"[\s\u00a0\u202f]+")
_UNITS = {"g": ("kg", 0.001), "kg": ("kg", 1.0), "ml": ("l", 0.001), "l": ("l", 1.0), "ks": ("pcs", 1.0),
          "m": ("m", 1.0)}
# number with , or . decimals + unit, nothing else; `N x size` / `N× size` multiply; `cca`-like prefixes ignored
_GRAMMAR = re.compile(
    r"^(?:(?:cca|ca|asi|approx)\.?\s*|~\s*)?"
    r"(?:(\d+)\s*[x×]\s*)?"
    r"(\d+(?:[.,]\d+)?)\s*(kg|g|ml|l|ks|m)$",
    re.IGNORECASE,
)


def parse(text: str | None) -> tuple[float, str] | None:
    """Return (amount, unit) with unit in kg/l/pcs/m, or None when the text doesn't parse."""
    if not text:
        return None
    norm = _SPACES.sub(" ", text).strip()
    m = _GRAMMAR.match(norm)
    if not m:
        return None
    count = int(m.group(1)) if m.group(1) else 1
    amount = float(m.group(2).replace(",", "."))
    unit, factor = _UNITS[m.group(3).lower()]
    value = count * amount * factor
    if value <= 0:
        return None
    return round(value, 9), unit


@dataclass(frozen=True)
class Size:
    value: float | None
    note: str | None = None  # None | "other_unit" | "unparsed" | "override_other_unit"
    text: str | None = None

    @property
    def known(self) -> bool:
        return self.value is not None


def size_of(text: str | None, base_unit: str, upp_override: float | None = None,
            upp_unit: str | None = None, weighted: bool = False) -> Size:
    """size(s, text) of §6.2 for a SKU of a tag with base unit `base_unit`. A weighed SKU in a `pcs` tag is one
    piece, whatever its weight and text, unless its override gives a piece count (a weighed bunch)."""
    if upp_override is not None:
        if upp_unit == base_unit:
            return Size(float(upp_override), None, text)
        # A stored size in another unit is never reinterpreted (§6.2).
        return Size(None, "override_other_unit", text)
    if weighted and base_unit == "pcs":
        return Size(1.0, None, text)
    parsed = parse(text)
    if parsed is None:
        return Size(None, "unparsed", text)
    amount, unit = parsed
    if unit != base_unit:
        return Size(None, "other_unit", text)
    return Size(amount, None, text)


def delivered_line_base(quantity: float | None, claimed: float, weight_kg: float | None, use_weight: bool,
                        size: float | None) -> float | None:
    """Base quantity of a delivered line (§6.2), including one delivered today. `use_weight` holds for a `kg` tag:
    its weighed lines count their actual weight; elsewhere, a weighed line in a `pcs` tag included, a line counts
    (quantity − claimed pieces) × size and its weight is ignored."""
    claimed = claimed or 0.0
    if use_weight and weight_kg is not None:
        if claimed > 0:
            if not quantity:
                return None
            return max(0.0, weight_kg * (1.0 - claimed / quantity))
        return max(0.0, weight_kg)
    if quantity is None or size is None:
        return None
    return max(0.0, (quantity - claimed) * size)


def gross_line_base(quantity: float | None, weight_kg: float | None, use_weight: bool,
                    size: float | None) -> float | None:
    """Gross base quantity before claims, for the stock-up assessment (§6.14)."""
    if use_weight and weight_kg is not None:
        return weight_kg
    if quantity is None or size is None:
        return None
    return quantity * size


def differing_texts(latest: str | None, line_texts: list[str | None], weighted: bool) -> str | None:
    """§7.1 step 6: a SKU's order-line texts when they differ from the latest one, e.g. "'2 l' in 3 orders, '500 ml'
    now" — a pack that changed size, or a text that was wrong (§10.4). Texts that parse to the same size are the same
    text. A weighed product's line text is that delivery's piece weight, which varies by nature and is counted by
    actual weight or in pieces (§6.2), so it is never listed."""
    if weighted:
        return None

    def key(text: str) -> tuple:
        parsed = parse(text)
        return ("size", parsed) if parsed else ("text", _SPACES.sub(" ", text).strip().lower())

    now = key(latest) if latest else None
    seen: dict[tuple, list] = {}
    for text in line_texts:
        if not text:
            continue
        k = key(text)
        if k == now:
            continue
        seen.setdefault(k, [text, 0])[1] += 1
    if not seen:
        return None
    parts = [f"'{text}' in {n} order{'s' if n != 1 else ''}" for text, n in
             sorted(seen.values(), key=lambda x: (-x[1], x[0]))]
    return ", ".join(parts) + (f", '{latest}' now" if latest else "")
