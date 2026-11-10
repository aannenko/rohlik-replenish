"""SQLite storage (spec §5): one database per account, durable inputs only."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from .constants import SCHEMA_VERSION, STORAGE_SEEDS
from .timeutil import iso

SCHEMA_V1 = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE storage_tags (
  name             TEXT PRIMARY KEY,
  stockup_eligible INTEGER NOT NULL CHECK (stockup_eligible IN (0,1)),
  updated_at       TEXT NOT NULL);

CREATE TABLE product_tags (
  tag_id       TEXT PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE CHECK (name = lower(name) AND length(name) BETWEEN 2 AND 64),
  policy       TEXT NOT NULL CHECK (policy IN ('cheapest','assorted','solo','variety')),
  base_unit    TEXT NOT NULL CHECK (base_unit IN ('kg','l','pcs','m')),
  stockup_mode TEXT CHECK (stockup_mode IN ('always','on_sale')),
  note         TEXT,
  source       TEXT NOT NULL CHECK (source IN ('agent','user')),
  updated_at   TEXT NOT NULL);

CREATE TABLE skus (
  sku            INTEGER PRIMARY KEY,
  name           TEXT NOT NULL,
  textual_amount TEXT,
  weighted       INTEGER NOT NULL DEFAULT 0 CHECK (weighted IN (0,1)),
  ignored        INTEGER NOT NULL DEFAULT 0 CHECK (ignored IN (0,1)),
  product_tag_id TEXT REFERENCES product_tags(tag_id) ON DELETE RESTRICT,
  storage_tag    TEXT REFERENCES storage_tags(name) ON DELETE RESTRICT,
  assigned_by    TEXT CHECK (assigned_by IN ('engine','agent','user')),
  upp_override   REAL CHECK (upp_override > 0),
  upp_unit       TEXT CHECK (upp_unit IN ('kg','l','pcs','m')),
  upp_source     TEXT CHECK (upp_source IN ('agent','user')),
  updated_at     TEXT NOT NULL,
  CHECK ((product_tag_id IS NULL) = (storage_tag IS NULL)),
  CHECK (ignored = 0 OR product_tag_id IS NULL),
  CHECK ((assigned_by IS NULL) = (ignored = 0 AND product_tag_id IS NULL)),
  CHECK (assigned_by <> 'engine' OR ignored = 1),
  CHECK ((upp_override IS NULL) = (upp_source IS NULL) AND (upp_override IS NULL) = (upp_unit IS NULL)));
CREATE INDEX skus_tag ON skus(product_tag_id);

CREATE TABLE orders (
  order_id      INTEGER PRIMARY KEY,
  order_time    TEXT NOT NULL,
  delivery_date TEXT NOT NULL,
  slot_end      TEXT,
  total_czk     REAL,
  fetched_at    TEXT NOT NULL);

CREATE TABLE order_lines (
  order_id         INTEGER NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
  sku              INTEGER NOT NULL REFERENCES skus(sku),
  quantity         REAL CHECK (quantity >= 0),
  weight_kg        REAL CHECK (weight_kg >= 0),
  textual_amount   TEXT,
  line_price_czk   REAL NOT NULL,
  claimed_quantity REAL NOT NULL DEFAULT 0 CHECK (claimed_quantity >= 0),
  PRIMARY KEY (order_id, sku));
CREATE INDEX order_lines_sku ON order_lines(sku);

CREATE TABLE cancelled_orders (
  order_id INTEGER PRIMARY KEY,
  seen_at  TEXT NOT NULL);

CREATE TABLE prefs (
  tag_id TEXT NOT NULL REFERENCES product_tags(tag_id) ON DELETE CASCADE,
  kind   TEXT NOT NULL CHECK (kind IN ('never')),
  at     TEXT NOT NULL,
  PRIMARY KEY (tag_id, kind));

CREATE TABLE runs (
  run_id       TEXT PRIMARY KEY,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  state        TEXT NOT NULL CHECK (state IN ('dry','writing','done','failed')),
  recovered    INTEGER NOT NULL DEFAULT 0 CHECK (recovered IN (0,1)),
  t_date       TEXT,
  cadence_days REAL,
  options      TEXT NOT NULL DEFAULT '{}',
  summary_md   TEXT);

CREATE TABLE run_upcoming_orders (
  run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  order_id      INTEGER NOT NULL,
  order_time    TEXT,
  delivery_date TEXT NOT NULL,
  slot_end      TEXT,
  sku           INTEGER NOT NULL REFERENCES skus(sku),
  amount        REAL NOT NULL,
  PRIMARY KEY (run_id, order_id, sku));
CREATE INDEX run_upcoming_order ON run_upcoming_orders(order_id);

CREATE TABLE run_plan (
  run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  sku           INTEGER NOT NULL REFERENCES skus(sku),
  reason        TEXT NOT NULL CHECK (reason IN ('due','suggest_gate','wait_sale','size_unknown',
                                                'basket_limit','unavailable','stockup','budget_limit','overdue')),
  normal_units  REAL NOT NULL DEFAULT 0 CHECK (normal_units >= 0),
  stockup_units REAL NOT NULL DEFAULT 0 CHECK (stockup_units >= 0),
  sale_until    TEXT,
  size          REAL CHECK (size > 0),
  size_unit     TEXT CHECK (size_unit IN ('kg','l','pcs','m')),
  PRIMARY KEY (run_id, sku));

CREATE TABLE run_cart (
  run_id            TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  sku               INTEGER NOT NULL REFERENCES skus(sku),
  before_qty        REAL NOT NULL,
  user_owned_before REAL NOT NULL,
  target_qty        REAL,
  after_qty         REAL,
  agent_owned       REAL,
  PRIMARY KEY (run_id, sku));

CREATE TABLE run_edits (
  run_id       TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  sku          INTEGER NOT NULL REFERENCES skus(sku),
  proposal_run TEXT NOT NULL REFERENCES runs(run_id),
  PRIMARY KEY (run_id, sku));

CREATE TABLE run_calls (
  run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,
  tool        TEXT NOT NULL,
  args_json   TEXT NOT NULL,
  duration_ms INTEGER NOT NULL,
  outcome     TEXT NOT NULL,
  PRIMARY KEY (run_id, seq));
"""

MIGRATIONS = {1: SCHEMA_V1}

# §5.2, version 2: `product_tags` gains the preferred product (§5.3) and its CHECKs. The 1 -> 2 step rebuilds the
# table under this definition; `skus` and `product_tags` now reference each other.
PRODUCT_TAGS_V2 = """
CREATE TABLE {table} (
  tag_id           TEXT PRIMARY KEY,
  name             TEXT NOT NULL UNIQUE CHECK (name = lower(name) AND length(name) BETWEEN 2 AND 64),
  policy           TEXT NOT NULL CHECK (policy IN ('cheapest','assorted','solo','variety')),
  base_unit        TEXT NOT NULL CHECK (base_unit IN ('kg','l','pcs','m')),
  stockup_mode     TEXT CHECK (stockup_mode IN ('always','on_sale')),
  note             TEXT,
  source           TEXT NOT NULL CHECK (source IN ('agent','user')),
  preferred_sku    INTEGER REFERENCES skus(sku),
  preferred_source TEXT CHECK (preferred_source IN ('agent','user')),
  updated_at       TEXT NOT NULL,
  CHECK ((policy = 'solo') = (preferred_sku IS NOT NULL)),
  CHECK ((preferred_sku IS NULL) = (preferred_source IS NULL)))
"""

TAG_COLUMNS_V1 = ("tag_id", "name", "policy", "base_unit", "stockup_mode", "note", "source", "updated_at")


class DbError(Exception):
    pass


def connect(path: str, now: datetime) -> sqlite3.Connection:
    """Open (and migrate) a database. `path` may be ':memory:' (tests)."""
    conn = sqlite3.connect(path, isolation_level=None)  # explicit transactions only
    conn.row_factory = sqlite3.Row
    _foreign_keys(conn, True)
    migrate(conn, now)
    return conn


def _foreign_keys(conn: sqlite3.Connection, on: bool) -> None:
    """§5.1: set before any transaction (the pragma does nothing inside one) and assert it reads back."""
    conn.execute(f"PRAGMA foreign_keys = {'ON' if on else 'OFF'}")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != (1 if on else 0):
        raise DbError(f"SQLite foreign keys could not be turned {'on' if on else 'off'}")


def migrate(conn: sqlite3.Connection, now: datetime) -> None:
    """Forward-only steps, each in its own transaction (§5.1)."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise DbError(f"database schema {version} is newer than this engine ({SCHEMA_VERSION})")
    if version < 1:
        with transaction(conn):
            for statement in _split(MIGRATIONS[1]):  # DDL runs inside the one transaction
                conn.execute(statement)
            conn.executemany(
                "INSERT INTO storage_tags(name, stockup_eligible, updated_at) VALUES (?,?,?)",
                [(name, flag, iso(now)) for name, flag in STORAGE_SEEDS])
            conn.execute("PRAGMA user_version = 1")
    if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
        _migrate_1_to_2(conn, now)


def _migrate_1_to_2(conn: sqlite3.Connection, now: datetime) -> None:
    """§5.1 1 -> 2 (preferred products). SQLite can't add the new CHECKs to a table that holds `solo` rows, nor
    rebuild a parent table while foreign keys are on, and the pragma does nothing inside a transaction. So this step
    alone turns foreign keys off before its transaction; it rebuilds `product_tags`, filling each `solo` tag's
    preferred product (§5.3), adds `run_plan.fallback_from`, requires an empty `foreign_key_check` before it commits,
    and sets `user_version` 2 and `preferred_check` (when it set preferred products, §8.1). A failure rolls it all
    back and leaves version 1; foreign keys are back on either way."""
    from .preferred import migration_fill
    from .timeutil import local_date

    _foreign_keys(conn, False)
    try:
        with transaction(conn):
            fill = migration_fill(conn, local_date(now))
            conn.execute(PRODUCT_TAGS_V2.format(table="product_tags_v2").strip())
            cols = ", ".join(TAG_COLUMNS_V1)
            for row in conn.execute(f"SELECT {cols} FROM product_tags ORDER BY tag_id").fetchall():
                values = dict(zip(TAG_COLUMNS_V1, row))
                preferred = fill.get(values["tag_id"]) if values["policy"] == "solo" else None
                if values["policy"] == "solo" and preferred is None:
                    values["policy"] = "cheapest"  # a solo tag without SKUs, which §5.3 never leaves behind
                conn.execute(f"INSERT INTO product_tags_v2({cols}, preferred_sku, preferred_source) "
                             f"VALUES ({', '.join('?' * len(TAG_COLUMNS_V1))}, ?, ?)",
                             (*values.values(), preferred, "agent" if preferred is not None else None))
            conn.execute("DROP TABLE product_tags")
            conn.execute("ALTER TABLE product_tags_v2 RENAME TO product_tags")
            conn.execute("ALTER TABLE run_plan ADD COLUMN fallback_from INTEGER REFERENCES skus(sku)")
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise DbError(f"the database has {len(broken)} broken references (first: table {broken[0][0]}, "
                              f"row {broken[0][1]}); it was left at version 1")
            if any(v is not None for v in fill.values()):
                set_meta(conn, "preferred_check", iso(now))
            conn.execute("PRAGMA user_version = 2")
    finally:
        _foreign_keys(conn, True)


def _split(script: str) -> list[str]:
    out, buf = [], []
    for line in script.splitlines():
        buf.append(line)
        if sqlite3.complete_statement("\n".join(buf)):
            stmt = "\n".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
    return out


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if conn.in_transaction:
        raise DbError("nested transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))
    else:
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, value))
