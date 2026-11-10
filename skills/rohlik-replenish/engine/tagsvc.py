"""Tag and preference commands (spec §5.3, §7.3, §9). Each command is one transaction; any violation
rolls back the whole change and reports precise errors."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from .constants import BASE_UNITS, POLICIES, STOCKUP_MODES, STORAGE_TAGS, SUMMARY_NAMES_MAX
from .db import get_meta, set_meta, transaction
from .sizes import differing_texts, parse, size_of
from .timeutil import iso

_WS = re.compile(r"\s+")


class TagError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class TagResult:
    messages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)


def normalise_name(name: str) -> str:
    return _WS.sub(" ", str(name)).strip().lower()


def _check_name(name: str) -> str | None:
    if not 2 <= len(name) <= 64:
        return f"tag name '{name}' must be 2-64 characters long"
    return None


def resolve_tag(conn: sqlite3.Connection, ref: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM product_tags WHERE tag_id = ?", (str(ref),)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM product_tags WHERE name = ?", (normalise_name(ref),)).fetchone()
    return row


def _require_tag(conn, ref: str) -> sqlite3.Row:
    row = resolve_tag(conn, ref)
    if row is None:
        raise TagError([f"no product tag '{ref}'"])
    return row


def _next_tag_id(conn: sqlite3.Connection, taken: set[str]) -> str:
    n = 0
    for (tid,) in conn.execute("SELECT tag_id FROM product_tags ORDER BY tag_id"):
        if tid.startswith("t") and tid[1:].isdigit():
            n = max(n, int(tid[1:]))
    for tid in taken:
        if tid.startswith("t") and tid[1:].isdigit():
            n = max(n, int(tid[1:]))
    return f"t{n + 1:04d}"


def _eligible(conn) -> dict[str, bool]:
    return {r[0]: bool(r[1]) for r in conn.execute("SELECT name, stockup_eligible FROM storage_tags ORDER BY name")}


def _tag_is_long_life(conn, tag_id: str) -> bool:
    elig = _eligible(conn)
    rows = conn.execute("SELECT storage_tag FROM skus WHERE product_tag_id = ? ORDER BY sku", (tag_id,)).fetchall()
    return bool(rows) and all(elig.get(r[0], False) for r in rows)


def _delete_empty_tags(conn, result: TagResult) -> None:
    rows = conn.execute(
        "SELECT t.tag_id, t.name FROM product_tags t WHERE NOT EXISTS "
        "(SELECT 1 FROM skus s WHERE s.product_tag_id = t.tag_id) ORDER BY t.name, t.tag_id").fetchall()
    for r in rows:
        prefs = [p[0] for p in conn.execute("SELECT kind FROM prefs WHERE tag_id = ? ORDER BY kind", (r["tag_id"],))]
        conn.execute("DELETE FROM prefs WHERE tag_id = ?", (r["tag_id"],))
        conn.execute("DELETE FROM product_tags WHERE tag_id = ?", (r["tag_id"],))
        msg = f"tag '{r['name']}' has no products left and was deleted"
        if prefs:
            msg += f"; its preferences were dropped: {', '.join(prefs)}"
        result.messages.append(msg)


def _size_warnings(conn, tag_id: str, base_unit: str) -> list[str]:
    out = []
    for s in conn.execute("SELECT sku, name, textual_amount, weighted, upp_override, upp_unit FROM skus "
                          "WHERE product_tag_id = ? ORDER BY sku", (tag_id,)):
        size = size_of(s["textual_amount"], base_unit, s["upp_override"], s["upp_unit"], bool(s["weighted"]))
        if not size.known:
            out.append(f"{s['name']} ({s['sku']}): size unknown in {base_unit} (text '{s['textual_amount']}') - "
                       f"give units_per_pack with tags apply")
    return out


def _counted_in_pieces(conn, tag_id: str) -> list[str]:
    """A tag that now counts its weighed products in pieces: each is 1 piece unless it has a piece count (§5.3)."""
    rows = conn.execute("SELECT sku, name, textual_amount, upp_override, upp_unit FROM skus WHERE product_tag_id = ? "
                        "AND weighted = 1 ORDER BY sku", (tag_id,)).fetchall()
    ones = [f"{r['name']} ({r['sku']}, '{r['textual_amount']}')" for r in rows
            if not (r["upp_override"] is not None and r["upp_unit"] == "pcs")]
    if not ones:
        return []
    return ["weighed products now counted as 1 piece each: " + _names(ones) + ". A bunch or pack of pieces needs "
            "its piece count as units_per_pack (tags apply)"]


WEIGHED_UNITS = ("kg", "pcs")  # §5.3: a weighed SKU counts by weight in a kg tag, in pieces in a pcs tag


# ------------------------------------------------------------------ preferred products (§5.3)
def _sku_name(conn, sku: int | None) -> str:
    row = conn.execute("SELECT name FROM skus WHERE sku = ?", (sku,)).fetchone() if sku is not None else None
    return f"{row[0]} ({sku})" if row else f"#{sku}"


def _preferred_snapshot(conn) -> dict[str, tuple[str, int, str]]:
    """Every solo tag: tag_id -> (name, preferred SKU, its source)."""
    return {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT tag_id, name, preferred_sku, preferred_source FROM product_tags WHERE policy = 'solo' "
        "ORDER BY tag_id")}


def _make_users(conn, sku: int, stamp: str) -> None:
    """§5.3: a preferred product set with `--source user` makes its SKU's assignment the user's."""
    conn.execute("UPDATE skus SET assigned_by = 'user', updated_at = ? WHERE sku = ? AND assigned_by <> 'user'",
                 (stamp, sku))


def _settle_preferred(conn, before: dict, source: str, stamp: str, result: TagResult) -> list[str]:
    """§5.3, judged on the transaction's end state, not entry by entry:
    - a solo tag whose preferred SKU is no longer one of its own SKUs (moved or ignored) becomes `cheapest`;
    - a preferred product the user set that the end state clears or changes rejects an agent-source command;
    - every preferred product a command drops or replaces is reported, per tag: a SKU that is now another tag's
      preferred product (a split, or two tags swapping theirs) still left this one. Only a tag that is gone
      and whose preferred product another tag adopted (a merge) has nothing to report.
    Returns the errors."""
    moved = set()
    for tag_id, name, sku in conn.execute(
            "SELECT t.tag_id, t.name, t.preferred_sku FROM product_tags t WHERE t.policy = 'solo' AND NOT EXISTS "
            "(SELECT 1 FROM skus s WHERE s.sku = t.preferred_sku AND s.product_tag_id = t.tag_id) "
            "ORDER BY t.name, t.tag_id").fetchall():
        conn.execute("UPDATE product_tags SET policy = 'cheapest', preferred_sku = NULL, preferred_source = NULL, "
                     "source = ?, updated_at = ? WHERE tag_id = ?", (source, stamp, tag_id))
        moved.add(tag_id)
    after = _preferred_snapshot(conn)
    kept = {sku for _, sku, _ in after.values()}
    errors = []
    for tag_id, (name, sku, src) in sorted(before.items(), key=lambda kv: (kv[1][0], kv[0])):
        now = after.get(tag_id)
        if now is not None and now[1] == sku:
            continue  # this tag still prefers it
        if src == "user" and source == "agent":
            errors.append(f"'{name}': the user chose {_sku_name(conn, sku)} as its preferred product; the agent may "
                          f"not clear or change it (use --source user)")
            continue
        row = conn.execute("SELECT name, policy, preferred_sku FROM product_tags WHERE tag_id = ?",
                           (tag_id,)).fetchone()
        if row is None and sku in kept:
            continue  # the tag is gone and another tag adopted its preferred product (a merge): nothing was dropped
        if tag_id in moved and row is not None:
            # also when its preferred product became another tag's (a split): §5.3 still reports the demotion
            result.messages.append(f"{row['name']} now buys the cheapest: its preferred product moved; name another "
                                   f"to keep it `solo`")
        elif row is None:
            result.messages.append(f"'{name}' is gone, and with it its preferred product {_sku_name(conn, sku)}")
        elif row["policy"] == "solo":
            result.messages.append(f"'{row['name']}': preferred product is now {_sku_name(conn, row['preferred_sku'])}"
                                   f" (was {_sku_name(conn, sku)})")
        else:
            result.messages.append(f"'{row['name']}' is {row['policy']} now: {_sku_name(conn, sku)} is no longer its "
                                   f"preferred product")
    return errors


def _names(items: list[str]) -> str:
    """§9: a command summary names at most 10 items."""
    shown = ", ".join(items[:SUMMARY_NAMES_MAX])
    return shown + (f" and {len(items) - SUMMARY_NAMES_MAX} more" if len(items) > SUMMARY_NAMES_MAX else "")


# ------------------------------------------------------------------ tags apply
_ENTRY_KEYS = {"sku", "product_tag", "policy", "base_unit", "storage_tag", "stockup_mode", "units_per_pack", "preferred",
               "ignore"}


def tags_apply(conn: sqlite3.Connection, entries, source: str, now: datetime) -> TagResult:
    if source not in ("agent", "user"):
        raise TagError([f"--source must be agent or user, not '{source}'"])
    if not isinstance(entries, list):
        raise TagError(["the file must hold a JSON array of entries"])
    errors: list[str] = []
    result = TagResult()
    stamp = iso(now)
    with transaction(conn):
        before = _preferred_snapshot(conn)
        new_tags: dict[str, dict] = {}  # name -> attrs of tags created by this file
        seen_skus: set[int] = set()
        planned: list[tuple[int, dict, str | None]] = []  # (sku, entry, tag name or None for ignore)
        preferred: dict[str, list[int]] = {}  # tag name -> SKUs marked preferred: true
        for idx, entry in enumerate(entries):
            where = f"entry {idx + 1}"
            if not isinstance(entry, dict):
                errors.append(f"{where}: not an object")
                continue
            unknown = set(entry) - _ENTRY_KEYS
            if unknown:
                errors.append(f"{where}: unknown keys {sorted(unknown)}")
            try:
                sku = int(entry.get("sku"))
            except (TypeError, ValueError):
                errors.append(f"{where}: 'sku' must be a product ID")
                continue
            where = f"sku {sku}"
            if sku in seen_skus:
                errors.append(f"{where}: listed twice")
                continue
            seen_skus.add(sku)
            row = conn.execute("SELECT * FROM skus WHERE sku = ?", (sku,)).fetchone()
            if row is None:
                errors.append(f"{where}: unknown product (never seen in orders or the cart)")
                continue
            if row["assigned_by"] == "user" and source == "agent":
                errors.append(f"{where}: assigned by the user; the agent may not change it (use --source user)")
                continue
            if entry.get("preferred") not in (None, False, True):
                errors.append(f"{where}: 'preferred' must be true or false")
                continue
            if entry.get("ignore") is True:
                if entry.get("preferred") is True:
                    errors.append(f"{where}: an ignored product can't be a tag's preferred product")
                    continue
                planned.append((sku, entry, None))
                continue
            if entry.get("ignore") not in (None, False):
                errors.append(f"{where}: 'ignore' must be true or false")
                continue
            name = entry.get("product_tag")
            storage = entry.get("storage_tag")
            if not isinstance(name, str) or not name.strip():
                errors.append(f"{where}: 'product_tag' is required")
                continue
            if storage not in STORAGE_TAGS:
                errors.append(f"{where}: 'storage_tag' must be one of {', '.join(STORAGE_TAGS)}")
                continue
            existing = resolve_tag(conn, name)
            norm = existing["name"] if existing else normalise_name(name)
            policy, unit, mode = entry.get("policy"), entry.get("base_unit"), entry.get("stockup_mode")
            if policy is not None and policy not in POLICIES:
                errors.append(f"{where}: policy must be one of {', '.join(POLICIES)}")
                continue
            if unit is not None and unit not in BASE_UNITS:
                errors.append(f"{where}: base_unit must be one of {', '.join(BASE_UNITS)}")
                continue
            if mode is not None and mode not in STOCKUP_MODES:
                errors.append(f"{where}: stockup_mode must be always or on_sale")
                continue
            if existing is not None:
                attrs = {"policy": existing["policy"], "base_unit": existing["base_unit"],
                         "stockup_mode": existing["stockup_mode"]}
                for key, value in (("policy", policy), ("base_unit", unit)):
                    if value is not None and value != attrs[key]:
                        errors.append(f"{where}: tag '{norm}' has {key} {attrs[key]}; change it with tags set")
                if mode is not None and attrs["stockup_mode"] is not None and mode != attrs["stockup_mode"]:
                    errors.append(f"{where}: tag '{norm}' has stock-up mode {attrs['stockup_mode']}; "
                                  f"change it with tags set")
                elif mode is not None and attrs["stockup_mode"] is None:
                    spec = new_tags.setdefault(norm, {})
                    if spec.get("mode_update") not in (None, mode):
                        errors.append(f"{where}: conflicting stockup_mode for tag '{norm}'")
                    spec["mode_update"] = mode
            else:
                bad = _check_name(norm)
                if bad:
                    errors.append(f"{where}: {bad}")
                    continue
                # A new tag's attributes may be given on any of its entries; they must agree.
                spec = new_tags.setdefault(norm, {"new": True, "policy": None, "base_unit": None,
                                                  "stockup_mode": None})
                for key, value in (("policy", policy), ("base_unit", unit), ("stockup_mode", mode)):
                    if value is not None and spec.get(key) is not None and value != spec[key]:
                        errors.append(f"{where}: conflicting {key} for new tag '{norm}'")
                    elif value is not None:
                        spec[key] = value
            if entry.get("preferred") is True:
                preferred.setdefault(norm, []).append(sku)
            planned.append((sku, entry, norm))
        for name in sorted(new_tags):
            spec = new_tags[name]
            if spec.get("new") and (spec["policy"] is None or spec["base_unit"] is None):
                errors.append(f"new tag '{name}' needs policy and base_unit")
        # §7.3: a new solo tag needs exactly one preferred entry; on an existing tag only the user names one
        for name in sorted(set(new_tags) | set(preferred)):
            marked = preferred.get(name, [])
            spec = new_tags.get(name, {})
            if spec.get("new"):
                if spec.get("policy") == "solo" and len(marked) != 1:
                    errors.append(f"new solo tag '{name}' needs exactly one entry with preferred: true (it has "
                                  f"{len(marked)})")
                elif spec.get("policy") not in (None, "solo") and marked:
                    errors.append(f"tag '{name}' is {spec['policy']}: preferred: true marks the product a solo tag "
                                  f"buys")
            elif marked:
                existing = resolve_tag(conn, name)
                if source == "agent":
                    errors.append(f"tag '{name}' already exists: only the user names its preferred product "
                                  f"(--source user)")
                elif len(marked) > 1:
                    errors.append(f"tag '{name}': {len(marked)} entries with preferred: true; a solo tag has one")
                elif existing["policy"] != "solo":
                    errors.append(f"tag '{name}' is {existing['policy']}: make it solo with tags set '{name}' "
                                  f"--preferred {marked[0]} --source user")
        if errors:
            raise TagError(errors)

        # Create new tags, then assign.
        name_to_id: dict[str, str] = {}
        created: set[str] = set()
        for name in sorted(new_tags):
            spec = new_tags[name]
            if not spec.get("new"):
                continue
            tid = _next_tag_id(conn, created)
            created.add(tid)
            pref = preferred[name][0] if spec["policy"] == "solo" else None
            conn.execute("INSERT INTO product_tags(tag_id, name, policy, base_unit, stockup_mode, source, "
                         "preferred_sku, preferred_source, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (tid, name, spec["policy"], spec["base_unit"], None, source, pref,
                          source if pref is not None else None, stamp))
            name_to_id[name] = tid
        if name_to_id:
            result.messages.append(f"created {len(name_to_id)} tag(s): " + _names(
                [f"{n} ({new_tags[n]['policy']}, {new_tags[n]['base_unit']})" for n in sorted(name_to_id)]))
        for sku, entry, name in planned:
            row = conn.execute("SELECT * FROM skus WHERE sku = ?", (sku,)).fetchone()
            if name is None:
                conn.execute("UPDATE skus SET ignored = 1, product_tag_id = NULL, storage_tag = NULL, assigned_by = ?, "
                             "updated_at = ? WHERE sku = ?", (source, stamp, sku))
                continue
            tag = resolve_tag(conn, name)
            tid = tag["tag_id"]
            unit = tag["base_unit"]
            if row["weighted"] and unit not in WEIGHED_UNITS:
                errors.append(f"sku {sku}: a weighed product goes in a kg tag (counted by weight) or a pcs tag "
                              f"(counted in pieces), not '{name}' ({unit})")
                continue
            override, o_unit, o_source = row["upp_override"], row["upp_unit"], row["upp_source"]
            if "units_per_pack" in entry:
                upp = entry["units_per_pack"]
                if o_source == "user" and source == "agent" and upp != override:
                    errors.append(f"sku {sku}: its pack size was set by the user; the agent may not change it")
                    continue
                if upp is None:
                    override, o_unit, o_source = None, None, None
                elif isinstance(upp, (int, float)) and not isinstance(upp, bool) and upp > 0:
                    override, o_unit, o_source = float(upp), unit, source
                else:
                    errors.append(f"sku {sku}: units_per_pack must be a positive number in {unit}, or null")
                    continue
            else:
                has_override = override is not None and o_unit == unit
                # §5.3: a weighed SKU in a pcs tag is exempt; its text is a weight and it counts 1 per piece
                pieces = bool(row["weighted"]) and unit == "pcs"
                parsed = parse(row["textual_amount"])
                if not has_override and not pieces and (parsed is None or parsed[1] != unit):
                    what = "doesn't parse" if parsed is None else f"parses in {parsed[1]}"
                    errors.append(f"sku {sku} ({row['name']}): text '{row['textual_amount']}' {what}, not {unit}; "
                                  f"give units_per_pack (a number in {unit}) or null")
                    continue
                if override is not None and o_unit != unit:  # never reinterpreted: its size is unknown (§6.2)
                    result.warnings.append(f"sku {sku} ({row['name']}): its pack size was given in {o_unit}, so it "
                                           f"is unknown in {unit}; give units_per_pack (a number in {unit}) or null")
            conn.execute("UPDATE skus SET ignored = 0, product_tag_id = ?, storage_tag = ?, assigned_by = ?, "
                         "upp_override = ?, upp_unit = ?, upp_source = ?, updated_at = ? WHERE sku = ?",
                         (tid, entry["storage_tag"], source, override, o_unit, o_source, stamp, sku))
        if errors:
            raise TagError(errors)
        # Stock-up modes, after every SKU moved (rejected for a tag that is short-life after the change).
        for name in sorted(new_tags):
            spec = new_tags[name]
            mode = spec.get("stockup_mode") or spec.get("mode_update")
            if not mode:
                continue
            tag = resolve_tag(conn, name)
            if tag is None:
                continue
            if not _tag_is_long_life(conn, tag["tag_id"]):
                errors.append(f"tag '{name}': a stock-up mode needs a long-life tag (every product stored "
                              f"stock-up-eligible); it is short-life")
                continue
            conn.execute("UPDATE product_tags SET stockup_mode = ?, source = ?, updated_at = ? WHERE tag_id = ?",
                         (mode, source, stamp, tag["tag_id"]))
        if errors:
            raise TagError(errors)
        # The user names an existing solo tag's new preferred product (§5.3, §7.3).
        for name, marked in sorted(preferred.items()):
            if name in name_to_id:
                continue
            tag = resolve_tag(conn, name)
            conn.execute("UPDATE product_tags SET preferred_sku = ?, preferred_source = 'user', source = ?, "
                         "updated_at = ? WHERE tag_id = ?", (marked[0], source, stamp, tag["tag_id"]))
        if source == "user":
            for marked in preferred.values():
                _make_users(conn, marked[0], stamp)
        _delete_empty_tags(conn, result)
        errors.extend(_settle_preferred(conn, before, source, stamp, result))
        if errors:
            raise TagError(errors)
    result.messages.append(f"assigned {len(planned)} products")
    result.data = {"assigned": len(planned), "created": sorted(name_to_id)}
    return result


# ------------------------------------------------------------------ other tag commands
def tags_rename(conn, ref: str, new_name: str, source: str, now: datetime) -> TagResult:
    result = TagResult()
    with transaction(conn):
        tag = _require_tag(conn, ref)
        name = normalise_name(new_name)
        bad = _check_name(name)
        if bad:
            raise TagError([bad])
        clash = conn.execute("SELECT tag_id FROM product_tags WHERE name = ? AND tag_id <> ?",
                             (name, tag["tag_id"])).fetchone()
        if clash:
            raise TagError([f"a tag named '{name}' already exists (merge it instead)"])
        conn.execute("UPDATE product_tags SET name = ?, source = ?, updated_at = ? WHERE tag_id = ?",
                     (name, source, iso(now), tag["tag_id"]))
    result.messages.append(f"renamed '{tag['name']}' to '{name}' (tag_id {tag['tag_id']} kept)")
    return result


def tags_merge(conn, from_ref: str, into_ref: str, source: str, now: datetime) -> TagResult:
    """§5.3: INTO keeps its policy, preferred product, base unit and mode (taking FROM's mode when it has none),
    except that a preferred product the user set in FROM wins over one the agent set in INTO. Every preferred product
    the merge drops is reported."""
    result = TagResult()
    stamp = iso(now)
    with transaction(conn):
        before = _preferred_snapshot(conn)
        src = _require_tag(conn, from_ref)
        dst = _require_tag(conn, into_ref)
        if src["tag_id"] == dst["tag_id"]:
            raise TagError(["FROM and INTO are the same tag"])
        errors = []
        skus = conn.execute("SELECT * FROM skus WHERE product_tag_id = ? ORDER BY sku", (src["tag_id"],)).fetchall()
        for s in skus:
            if s["weighted"] and dst["base_unit"] not in WEIGHED_UNITS:
                errors.append(f"sku {s['sku']}: a weighed product goes in a kg or a pcs tag; '{dst['name']}' is "
                              f"{dst['base_unit']}")
            if s["assigned_by"] == "user" and source == "agent":
                errors.append(f"sku {s['sku']}: assigned by the user; the agent may not move it (use --source user)")
        if errors:
            raise TagError(errors)
        conn.execute("UPDATE skus SET product_tag_id = ?, assigned_by = ?, updated_at = ? WHERE product_tag_id = ?",
                     (dst["tag_id"], source, stamp, src["tag_id"]))
        for p in conn.execute("SELECT kind, at FROM prefs WHERE tag_id = ? ORDER BY kind", (src["tag_id"],)).fetchall():
            conn.execute("INSERT OR IGNORE INTO prefs(tag_id, kind, at) VALUES (?,?,?)", (dst["tag_id"], p[0], p[1]))
        conn.execute("DELETE FROM prefs WHERE tag_id = ?", (src["tag_id"],))
        mode = dst["stockup_mode"] or src["stockup_mode"]
        conn.execute("UPDATE product_tags SET stockup_mode = ?, source = ?, updated_at = ? WHERE tag_id = ?",
                     (mode, source, stamp, dst["tag_id"]))
        if (src["policy"] == "solo" and src["preferred_source"] == "user" and dst["policy"] == "solo"
                and dst["preferred_source"] == "agent"):
            conn.execute("UPDATE product_tags SET preferred_sku = ?, preferred_source = 'user' WHERE tag_id = ?",
                         (src["preferred_sku"], dst["tag_id"]))
        conn.execute("DELETE FROM product_tags WHERE tag_id = ?", (src["tag_id"],))
        result.warnings.extend(_size_warnings(conn, dst["tag_id"], dst["base_unit"]))
        if dst["base_unit"] == "pcs" and any(s["weighted"] for s in skus):
            result.warnings.extend(_counted_in_pieces(conn, dst["tag_id"]))
        result.messages.append(f"merged '{src['name']}' into '{dst['name']}' ({len(skus)} products moved)")
        errors = _settle_preferred(conn, before, source, stamp, result)
        if errors:
            raise TagError(errors)
    return result


def tags_set(conn, ref: str, *, policy=None, stockup_mode=None, note=None, base_unit=None, preferred=None,
             source: str, now: datetime) -> TagResult:
    """§5.3, §9. `--preferred SKU` names one of the tag's SKUs and makes the tag `solo`; only the user names it on
    an existing tag. `--policy solo` needs a preferred product, and `--preferred` goes with no other policy. Another
    policy clears the preferred product."""
    result = TagResult()
    stamp = iso(now)
    with transaction(conn):
        before = _preferred_snapshot(conn)
        tag = _require_tag(conn, ref)
        errors = []
        if policy is not None and policy not in POLICIES:
            errors.append(f"policy must be one of {', '.join(POLICIES)}")
        if stockup_mode is not None and stockup_mode not in STOCKUP_MODES:
            errors.append("stock-up mode must be always or on_sale")
        if base_unit is not None and base_unit not in BASE_UNITS:
            errors.append(f"base unit must be one of {', '.join(BASE_UNITS)}")
        if base_unit is not None and base_unit not in WEIGHED_UNITS:
            heavy = conn.execute("SELECT sku FROM skus WHERE product_tag_id = ? AND weighted = 1 ORDER BY sku",
                                 (tag["tag_id"],)).fetchall()
            if heavy:
                errors.append(f"weighed products ({', '.join(str(r[0]) for r in heavy)}) need a kg tag (counted by "
                              f"weight) or a pcs tag (counted in pieces)")
        if stockup_mode is not None and not _tag_is_long_life(conn, tag["tag_id"]):
            errors.append(f"'{tag['name']}' is short-life (a product of it is not stored stock-up-eligible): "
                          f"it is bought one delivery at a time and has no stock-up mode")
        pref_sku = None
        if preferred is not None:
            try:
                pref_sku = int(preferred)
            except (TypeError, ValueError):
                errors.append(f"--preferred takes a product ID, not '{preferred}'")
            if policy is not None and policy != "solo":
                errors.append(f"--preferred makes the tag solo; it can't go with --policy {policy}")
            if source != "user":
                errors.append("only the user names the preferred product of an existing tag (--source user)")
            if pref_sku is not None:
                row = conn.execute("SELECT product_tag_id FROM skus WHERE sku = ?", (pref_sku,)).fetchone()
                if row is None or row[0] != tag["tag_id"]:
                    errors.append(f"product {pref_sku} isn't in '{tag['name']}': move it there first "
                                  f"(tags apply --source user)")
        elif policy == "solo" and tag["policy"] != "solo":
            errors.append(f"--policy solo needs --preferred SKU: the product '{tag['name']}' buys")
        if errors:
            raise TagError(errors)
        new_policy = "solo" if pref_sku is not None else (policy or tag["policy"])
        if new_policy != "solo":
            new_pref, new_pref_source = None, None
        elif pref_sku is not None:
            new_pref, new_pref_source = pref_sku, "user"
        else:
            new_pref, new_pref_source = tag["preferred_sku"], tag["preferred_source"]
        new = {"stockup_mode": stockup_mode or tag["stockup_mode"], "base_unit": base_unit or tag["base_unit"],
               "note": tag["note"] if note is None else (note.strip() or None)}
        conn.execute("UPDATE product_tags SET policy = ?, preferred_sku = ?, preferred_source = ?, stockup_mode = ?, "
                     "base_unit = ?, note = ?, source = ?, updated_at = ? WHERE tag_id = ?",
                     (new_policy, new_pref, new_pref_source, new["stockup_mode"], new["base_unit"], new["note"],
                      source, stamp, tag["tag_id"]))
        if pref_sku is not None:
            _make_users(conn, pref_sku, stamp)
        if base_unit is not None and base_unit != tag["base_unit"]:
            result.warnings.extend(_size_warnings(conn, tag["tag_id"], base_unit))
            if base_unit == "pcs":
                result.warnings.extend(_counted_in_pieces(conn, tag["tag_id"]))
        changed = [f"{k} {v}" for k, v in (("policy", policy if pref_sku is None else "solo"),
                                            ("stock-up mode", stockup_mode), ("base unit", base_unit)) if v is not None]
        if pref_sku is not None:
            changed.append(f"preferred product {_sku_name(conn, pref_sku)}")
        if note is not None:
            changed.append("note")
        result.messages.append(f"'{tag['name']}': set {', '.join(changed) if changed else 'nothing'}")
        if pref_sku is not None and tag["policy"] in ("variety", "assorted"):
            result.messages.append(f"'{tag['name']}' was {tag['policy']}: its mix ends, and it buys "
                                   f"{_sku_name(conn, pref_sku)} from now on")
        errors = _settle_preferred(conn, before, source, stamp, result)
        if errors:
            raise TagError(errors)
    return result


def tags_storage_set(conn, name: str, flag: bool, now: datetime) -> TagResult:
    with transaction(conn):
        row = conn.execute("SELECT * FROM storage_tags WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise TagError([f"no storage tag '{name}' (the set is fixed: {', '.join(STORAGE_TAGS)})"])
        conn.execute("UPDATE storage_tags SET stockup_eligible = ?, updated_at = ? WHERE name = ?",
                     (1 if flag else 0, iso(now), name))
    return TagResult([f"storage tag '{name}': stock-up {'yes' if flag else 'no'}"])


def prefs_never(conn, action: str, ref: str, now: datetime) -> TagResult:
    with transaction(conn):
        tag = _require_tag(conn, ref)
        if action == "add":
            conn.execute("INSERT OR IGNORE INTO prefs(tag_id, kind, at) VALUES (?, 'never', ?)",
                         (tag["tag_id"], iso(now)))
            msg = f"'{tag['name']}' will never be ordered"
        elif action == "remove":
            conn.execute("DELETE FROM prefs WHERE tag_id = ? AND kind = 'never'", (tag["tag_id"],))
            msg = f"'{tag['name']}' removed from the never list"
        else:
            raise TagError(["use: prefs never add|remove TAG"])
    return TagResult([msg])


def prefs_cadence(conn, value: str) -> TagResult:
    with transaction(conn):
        if value == "auto":
            set_meta(conn, "cadence_days", None)
            return TagResult(["cadence: automatic (median of your recent delivery gaps)"])
        try:
            days = float(value)
        except ValueError:
            raise TagError(["cadence must be a number of days or 'auto'"]) from None
        if not 1 <= days <= 60:
            raise TagError(["cadence must be between 1 and 60 days"])
        set_meta(conn, "cadence_days", repr(days))
    return TagResult([f"cadence: every {days:g} days"])


# ------------------------------------------------------------------ tags list
def tags_list(conn, ref: str | None = None) -> str:
    elig = _eligible(conn)
    if ref:
        tags = [_require_tag(conn, ref)]
    else:
        tags = conn.execute("SELECT * FROM product_tags ORDER BY name, tag_id").fetchall()
    times = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT l.sku, COUNT(*), MAX(o.delivery_date) FROM order_lines l JOIN orders o ON o.order_id = l.order_id "
        "GROUP BY l.sku ORDER BY l.sku")}
    prefs = {}
    for r in conn.execute("SELECT tag_id, kind FROM prefs ORDER BY tag_id, kind"):
        prefs.setdefault(r[0], []).append(r[1])
    line_texts: dict[int, list] = {}
    for sku, text in conn.execute("SELECT sku, textual_amount FROM order_lines ORDER BY sku, order_id"):
        line_texts.setdefault(sku, []).append(text)
    lines = ["# Product tags", ""]
    # §6.14: the first run guesses a long-life tag's mode from prices; after setup, a tag gets `always` at n >= 3
    unset_mode = ("none yet: `always` once bought 3 times" if get_meta(conn, "setup_completed_at")
                  else "the first run guesses it from your prices")
    for t in tags:
        skus = conn.execute("SELECT * FROM skus WHERE product_tag_id = ? ORDER BY name, sku", (t["tag_id"],)).fetchall()
        long_life = bool(skus) and all(elig.get(s["storage_tag"], False) for s in skus)
        mode = (t["stockup_mode"] or unset_mode) if long_life else None
        counting = None
        if any(s["weighted"] for s in skus):  # §5.3: how the tag counts its weighed products
            counting = "weighed products counted in pieces" if t["base_unit"] == "pcs" else \
                "weighed products counted by weight"
        policy = t["policy"]
        if t["policy"] == "solo":  # §9: a solo tag's preferred product
            policy = f"solo, prefers {_sku_name(conn, t['preferred_sku'])} (set by {t['preferred_source']})"
        head = f"## {t['name']} · {policy} · {t['base_unit']} · " + (
            f"long-life, stock-up: {mode}" if long_life else "short-life") + (f" · {counting}" if counting else "")
        lines.append(head)
        meta = [f"tag_id {t['tag_id']}", f"source {t['source']}"]
        if t["note"]:
            meta.append(f"note: {t['note']}")
        if prefs.get(t["tag_id"]):
            meta.append("prefs: " + ", ".join(prefs[t["tag_id"]]))
        lines.append("_" + " · ".join(meta) + "_")
        for s in skus:
            size = size_of(s["textual_amount"], t["base_unit"], s["upp_override"], s["upp_unit"], bool(s["weighted"]))
            n, last = times.get(s["sku"], (0, None))
            size_txt = f"{size.value:g} {t['base_unit']}" if size.known else "size unknown"
            if s["upp_override"] is not None:
                size_txt += f" (set by {s['upp_source']})"
            flags = " ⚖" if s["weighted"] else ""
            if t["policy"] == "solo" and s["sku"] == t["preferred_sku"]:
                flags += " · **preferred**"
            texts = differing_texts(s["textual_amount"], line_texts.get(s["sku"], []), bool(s["weighted"]))
            lines.append(f"- {s['name']} ({s['sku']}){flags} · '{s['textual_amount'] or ''}' → {size_txt} · "
                         f"{s['storage_tag']} · bought {n}×" + (f", last {last}" if last else "")
                         + f" · by {s['assigned_by']}" + (f" · order-line texts differ: {texts}" if texts else ""))
        lines.append("")
    if not ref:
        ignored = conn.execute("SELECT sku, name, assigned_by FROM skus WHERE ignored = 1 ORDER BY name, sku").fetchall()
        if ignored:
            lines.append("## Ignored products")
            lines.extend(f"- {r['name']} ({r['sku']}) · by {r['assigned_by']}" for r in ignored)
            lines.append("")
        waiting = conn.execute("SELECT sku, name FROM skus WHERE assigned_by IS NULL ORDER BY name, sku").fetchall()
        if waiting:
            lines.append("## Waiting for tags")
            lines.extend(f"- {r['name']} ({r['sku']})" for r in waiting)
            lines.append("")
    lines.append("## Storage tags")
    lines.append("| storage tag | stock-up eligible |")
    lines.append("| --- | --- |")
    for name, flag in conn.execute("SELECT name, stockup_eligible FROM storage_tags ORDER BY name"):
        lines.append(f"| {name} | {'yes' if flag else 'no'} |")
    cadence = get_meta(conn, "cadence_days")
    if cadence and not ref:
        lines.append("")
        lines.append(f"Cadence override: every {float(cadence):g} days")
    return "\n".join(lines) + "\n"
