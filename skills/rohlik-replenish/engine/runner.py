"""One run (§7.1): sync, tagging handshake, cycles and recovery, planning, the write protocol (§6.15),
minimum order (§6.16), summary and finalisation (§5.4)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .choice import Live, parse_live
from .constants import (BOUGHT_WITH_MAX, BOUGHT_WITH_MIN_ORDERS, EPS, GATE_SUGGEST, LINE_REASONS, MIN_HISTORY_DAYS,
                        MIN_HISTORY_ORDERS, SLOT_DAYS, TOKEN_MIN_LIFETIME_S)
from .cycles import (CycleState, Ownership, build_placed, checkout_in_progress, checkout_pending, close_ownership,
                     current_ownership, proposing_run, recovery_values, resolve_cycles)
from .db import get_meta, set_meta, transaction
from .numerics import clamp
from .paths import AuthFile, Config, account_db_path
from .planner import PlanInput, PlanResult, build_events, build_plan, build_tag_data, cadence_of, targets
from .replay import replay, waiting_tags
from .rohlik import (KEYRING_ACTIONS, AuthRequired, BudgetExhausted, KeyringFailure, RohlikClient, ToolFailure,
                     TransportError)
from .sync import Cart, SyncError, parse_cart, store_upcoming_skus, sync_all, update_from_details
from .timeutil import iso, local_date, parse_date_loose
from .world import RunRecord, UpcomingRec, World, load_world


@dataclass
class Envelope:
    status: str
    summary_md: str
    data: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({"status": self.status, "summary_md": self.summary_md, "data": self.data,
                           "warnings": self.warnings}, ensure_ascii=False, sort_keys=False, indent=1)

    @property
    def exit_code(self) -> int:
        return {"ok": 0, "dry_run": 0, "needs_tags": 2, "auth_required": 2, "insufficient_history": 2}.get(
            self.status, 1)


class StageError(Exception):
    def __init__(self, stage: str, message: str, actions: list[str], wrote: bool = False):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.actions = actions
        self.wrote = wrote


@dataclass
class SessionInfo:
    has_refresh: bool = True
    access_expires_at: float | None = None  # epoch seconds

    def write_window_ok(self, now: datetime) -> bool:
        if self.has_refresh or self.access_expires_at is None:
            return True
        return self.access_expires_at - now.timestamp() >= TOKEN_MIN_LIFETIME_S


@dataclass
class Env:
    data_dir: Path
    client: RohlikClient
    now: datetime
    config: Config
    auth: AuthFile
    session: SessionInfo
    open_db: Callable[[str], sqlite3.Connection]
    fixtures: bool = False
    clock: Callable[[], datetime] | None = None  # live runs: the real clock for finish times

    def finish_time(self) -> datetime:
        if self.clock is None:
            return self.now
        return max(self.now, self.clock())

    def current_time(self) -> datetime:
        """The time now, e.g. at the start of the write phase; `now` with injected clocks."""
        return self.finish_time()


@dataclass
class WriteRecord:
    sku: int
    tool: str
    before: float
    target: float
    outcome: str  # confirmed | failed | skipped


@dataclass
class RunReport:
    run_id: str
    now: datetime
    e: date
    t: date | None = None
    t_source: str | None = None
    cadence: float | None = None
    world: World | None = None
    state: CycleState | None = None
    own: Ownership | None = None
    plan: PlanResult | None = None
    live: dict[int, Live] = field(default_factory=dict)
    cart: Cart | None = None
    targets: dict[int, float] = field(default_factory=dict)
    dry_requested: bool = False
    first_run: bool = False
    pending: RunRecord | None = None
    in_progress: bool = False
    different_account: bool = False
    warnings: list[str] = field(default_factory=list)
    overdue_deliveries: list[tuple[int, date]] = field(default_factory=list)
    vanished_kept: list[tuple[int, date]] = field(default_factory=list)
    vanished_dropped: list[int] = field(default_factory=list)
    writes: list[WriteRecord] = field(default_factory=list)
    skipped_tags: list[str] = field(default_factory=list)
    final_cart: Cart | None = None
    checkout_link: str | None = None
    xtra_remaining: int | None = None
    recovered_run: str | None = None
    prev_run: RunRecord | None = None
    replay: object | None = None
    attempted: set[int] = field(default_factory=set)
    min_discount: float = 0.35
    report_path: str | None = None  # the first run's full setup report (§8.2)

    @property
    def dry(self) -> bool:
        return self.dry_requested or self.first_run or self.pending is not None or self.in_progress


def _next_run_id(conn: sqlite3.Connection, now: datetime) -> str:
    n = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] + 1
    return f"r{n:06d}-{now.strftime('%Y%m%dT%H%M%S')}"


def _options_json(date_opt: date | None, dry: bool, fixtures: bool) -> str:
    return json.dumps({"date": date_opt.isoformat() if date_opt else None, "dry_run": dry, "fixtures": fixtures},
                      sort_keys=True)


# ------------------------------------------------------------------------------------------ persistence
def _insert_run(conn, run_id: str, now: datetime, state: str, t: date | None, cadence: float | None, options: str,
                summary: str | None = None, finished: datetime | None = None) -> None:
    conn.execute("INSERT INTO runs(run_id, started_at, finished_at, state, t_date, cadence_days, options, summary_md) "
                 "VALUES (?,?,?,?,?,?,?,?)", (run_id, iso(now), iso(finished) if finished else None, state,
                                              t.isoformat() if t else None, cadence, options, summary))


def _insert_upcoming(conn, run_id: str, upcoming: list) -> None:
    for u in sorted(upcoming, key=lambda x: x.order_id):
        for sku in sorted(u.amounts):
            conn.execute("INSERT OR REPLACE INTO run_upcoming_orders(run_id, order_id, order_time, delivery_date, "
                         "slot_end, sku, amount) VALUES (?,?,?,?,?,?,?)",
                         (run_id, u.order_id, iso(u.order_time) if u.order_time else None, u.delivery_date.isoformat(),
                          iso(u.slot_end) if u.slot_end else None, sku, u.amounts[sku]))


def _insert_calls(conn, run_id: str, client: RohlikClient, start: int) -> int:
    for c in client.calls[start:]:
        conn.execute("INSERT OR REPLACE INTO run_calls(run_id, seq, tool, args_json, duration_ms, outcome) "
                     "VALUES (?,?,?,?,?,?)", (run_id, c.seq, c.tool, c.args_json, c.duration_ms, c.outcome))
    return len(client.calls)


def _insert_plan(conn, run_id: str, plan: PlanResult) -> None:
    for r in plan.rows:
        conn.execute("INSERT INTO run_plan(run_id, sku, reason, normal_units, stockup_units, sale_until, size, "
                     "size_unit, fallback_from) VALUES (?,?,?,?,?,?,?,?,?)",
                     (run_id, r.sku, r.reason, r.normal_units, r.stockup_units,
                      r.sale_until.isoformat() if r.sale_until else None, r.size, r.size_unit if r.size else None,
                      r.fallback_from))


def _store_modes(conn, plan: PlanResult | None, now: datetime) -> None:
    """The stock-up modes this run set (§6.14) and, on the first run, its guessed preferred products (§6.13)."""
    if plan is None:
        return
    for tag_id in sorted(plan.assessed):
        mode = plan.assessed[tag_id].mode
        if mode is not None:
            conn.execute("UPDATE product_tags SET stockup_mode = ?, updated_at = ? WHERE tag_id = ? "
                         "AND stockup_mode IS NULL", (mode, iso(now), tag_id))
    for tag_id in sorted(plan.guesses):
        conn.execute("UPDATE product_tags SET policy = 'solo', preferred_sku = ?, preferred_source = 'agent', "
                     "updated_at = ? WHERE tag_id = ? AND policy = 'cheapest' AND source = 'agent'",
                     (plan.guesses[tag_id].sku, iso(now), tag_id))


def store_failed(conn, run_id: str, now: datetime, upcoming: list, options: str, client: RohlikClient,
                 summary: str, t: date | None = None) -> None:
    """A run that stopped before its write-ahead commit, with every upcoming order it read; their SKU
    rows are inserted here when the sync transaction never committed them."""
    with transaction(conn):
        store_upcoming_skus(conn, upcoming, now)
        _insert_run(conn, run_id, now, "failed", t, None, options, summary)
        _insert_upcoming(conn, run_id, upcoming)
        _insert_calls(conn, run_id, client, 0)


# ------------------------------------------------------------------------------------------ the run
def run(env: Env, date_opt: date | None, dry_opt: bool) -> Envelope:
    from . import summary as summ

    now = env.now
    e = local_date(now)
    client = env.client
    stage = "account"
    conn = None
    report = None
    upcoming_read: list = []
    options = _options_json(date_opt, dry_opt, env.fixtures)
    try:
        if date_opt is not None and date_opt < e:
            raise StageError("options", f"--date {date_opt.isoformat()} is in the past",
                             ["pick today or a later delivery day"])
        info = client.get_user_info()
        account_id = str(info["id"])
        xtra = ((info.get("xtra") or {}).get("limits") or {}).get("orders_without_min_remaining")
        auth = env.auth.load()
        different = auth.get("last_account_id") not in (None, account_id)
        env.auth.update(last_account_id=account_id)
        conn = env.open_db(account_id)
        run_id = _next_run_id(conn, now)
        report = RunReport(run_id, now, e, dry_requested=dry_opt, different_account=different,
                           xtra_remaining=int(xtra) if isinstance(xtra, (int, float)) else None,
                           min_discount=env.config.stockup_min_discount)
        report.warnings.extend(env.config.warnings)

        stage = "sync"
        sres = sync_all(conn, client, now, e, observed=upcoming_read)
        report.cart = sres.cart
        report.warnings.extend(sres.warnings)
        report.vanished_kept, report.vanished_dropped = sres.vanished_kept, sres.vanished_dropped

        stage = "history"
        recent = conn.execute("SELECT COUNT(*) FROM orders WHERE delivery_date < ? AND delivery_date > ?",
                              (e.isoformat(), (e - timedelta(days=MIN_HISTORY_DAYS)).isoformat())).fetchone()[0]
        if recent < MIN_HISTORY_ORDERS:
            md = summ.render_insufficient(recent)
            store_failed(conn, run_id, now, upcoming_read, options, client, md)
            return Envelope("insufficient_history", md, {"delivered_orders_last_183_days": recent, "needed": 3},
                            report.warnings)

        stage = "tags"
        waiting = _waiting_skus(conn, upcoming_read, sres.cart, e)
        if waiting:
            details = client.product_details(waiting)
            update_from_details(conn, details, now)
            listing = _needs_tags_listing(conn, waiting, details, e)
            md = summ.render_needs_tags(listing)
            store_failed(conn, run_id, now, upcoming_read, options, client, md)
            return Envelope("needs_tags", md, {"skus": listing}, report.warnings)

        stage = "cycles"
        world = load_world(conn)
        report.world = world
        report.first_run = world.setup_completed_at is None
        report.prev_run = world.runs[-1] if world.runs else None
        current = RunRecord(run_id, int(run_id[1:7]), now, None, "current", False, None, None, {})
        current_upcoming = [UpcomingRec(u.order_id, u.order_time, u.delivery_date, u.slot_end, dict(u.amounts))
                            for u in upcoming_read]
        placed = build_placed(world, current_upcoming, current, e)
        state = resolve_cycles(world, placed)
        cart_qty = sres.cart.quantities()
        _recover(conn, world, state, cart_qty, report)
        close_ownership(world, state)
        report.state = state
        report.pending = checkout_pending(world, state, cart_qty, now)
        if report.pending is not None:
            # §6.6: a checkout Rohlik hasn't published yet; stop after the reads with only the banner: no plan, no
            # edits, no writes. Stored dry, so L stays the reference run until the order appears.
            md = summ.render_pending(report)
            with transaction(conn):
                _insert_run(conn, run_id, now, "dry", None, None, options, md, finished=env.finish_time())
                _insert_upcoming(conn, run_id, upcoming_read)
                _insert_calls(conn, run_id, client, 0)
            return Envelope("dry_run", md, summ.pending_data(report), report.warnings)
        report.in_progress = checkout_in_progress(world, state, cart_qty, current) is not None
        own = current_ownership(state, cart_qty, detect=True)
        report.own = own

        stage = "details"
        ids = _detail_ids(world, state, e, now) | set(cart_qty)
        details = client.product_details(ids, require_any=True)
        update_from_details(conn, details, now)
        _apply_texts(world, details)  # the latest texts seen (§6.1), without reloading run records
        live = {sku: parse_live(sku, d, e, env.config.stockup_min_discount) for sku, d in details.items()}
        report.live = live

        stage = "plan"
        t, t_source = _delivery_date(client, date_opt, e, report)
        report.t, report.t_source = t, t_source
        cadence = cadence_of([o.delivery_date for o in world.orders.values() if o.delivery_date < e],
                             world.cadence_override)
        report.cadence = cadence
        rep = replay(world, state)
        wait = waiting_tags(world, state, e, now)
        limits = {s: ln.max_basket for s, ln in sres.cart.lines.items() if ln.max_basket is not None}
        inp = PlanInput(world, state, own, e, t, now, cadence, live, limits, rep, wait,
                        env.config.stockup_min_discount, env.config.stockup_budget_czk, first_run=report.first_run)
        plan = build_plan(inp)
        report.plan = plan
        report.replay = rep
        report.targets = targets(world, plan, own)
        for p in state.placed:
            if p.source == "upcoming" and p.active and p.delivery_date < e:
                report.overdue_deliveries.append((p.order_id, p.delivery_date))

        if report.dry:
            stage = "checkout_link"
            report.checkout_link = _checkout_link(client, report)
            if report.first_run:  # §8.2: the full setup report goes to its file; summary_md is the compact version
                stage = "summary"
                path = account_db_path(env.data_dir, account_id).parent / "setup-report.md"
                report.report_path = str(path)
                _write_text(path, summ.render_setup(report, compact=False))
            md = summ.render(report)
            with transaction(conn):
                _insert_run(conn, run_id, now, "dry", t, cadence, options, md, finished=env.finish_time())
                _insert_upcoming(conn, run_id, upcoming_read)
                _insert_plan(conn, run_id, plan)
                _insert_cart_rows(conn, run_id, report, dry=True)
                _store_modes(conn, plan, now)
                if report.first_run:
                    set_meta(conn, "setup_completed_at", iso(now))
                _insert_calls(conn, run_id, client, 0)
            return Envelope("dry_run", md, summ.data(report), report.warnings)

        stage = "write"
        if not env.session.write_window_ok(env.current_time()):  # at the start of the write phase
            md = ("**Login needed.** The Rohlik session expires in under a minute and no refresh token was issued, "
                  "so nothing was written. Run `login`, then run again.")
            store_failed(conn, run_id, now, upcoming_read, options, client, md, t)
            return Envelope("auth_required", md, {"wrote": False}, report.warnings)
        _write_protocol(env, conn, report, options, upcoming_read)

        stage = "checkout_link"
        report.checkout_link = _checkout_link(client, report)  # a failed read is only a warning (§7.1 step 12)
        md = summ.render(report)
        _finalise(conn, report, md, client, env.finish_time())
        return Envelope("ok", md, summ.data(report), report.warnings)
    except AuthRequired:
        md = "**Login needed.** There is no valid Rohlik session. Run `login`, finish it in the browser, then run again."
        if conn is not None and report is not None:
            _safe_store_failed(conn, report, upcoming_read, options, client, md)
        return Envelope("auth_required", md, {"wrote": client.wrote}, [])
    except StageError as exc:
        return _failure(conn, report, upcoming_read, options, client, exc.stage, exc.message, exc.actions)
    except (ToolFailure, TransportError, SyncError) as exc:
        return _failure(conn, report, upcoming_read, options, client, stage, str(exc), _actions_for(stage, exc))
    except KeyringFailure as exc:
        return _failure(conn, report, upcoming_read, options, client, stage, str(exc), KEYRING_ACTIONS)
    except Exception as exc:  # noqa: BLE001 - §8.3: exactly one envelope, also when the engine fails unexpectedly
        return _failure(conn, report, upcoming_read, options, client, "internal", f"{type(exc).__name__}: {exc}",
                        ["this is a fault in the skill, not in your cart: run again, and if it keeps failing, report "
                         "the message"])


def _failure(conn, report, upcoming, options, client, stage: str, message: str, actions: list[str]) -> Envelope:
    """An `error` envelope; a run that failed before its write-ahead commit is stored as `failed`. `wrote` says
    whether a cart write was attempted."""
    if conn is not None and report is not None:
        _safe_store_failed(conn, report, upcoming, options, client, _error_md(stage, message, client.wrote, actions))
    return error_envelope(stage, message, client.wrote, actions)


def _safe_store_failed(conn, report, upcoming, options, client, md) -> None:
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        exists = conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (report.run_id,)).fetchone()
        if not exists:
            store_failed(conn, report.run_id, report.now, upcoming, options, client, md, report.t)
    except sqlite3.Error:
        pass


def _actions_for(stage: str, exc: Exception) -> list[str]:
    if isinstance(exc, BudgetExhausted):
        return ["Rohlik is answering too slowly: try again later"]
    if isinstance(exc, TransportError):
        return ["Rohlik didn't answer: try again in a few minutes"]
    if isinstance(exc, SyncError):
        return ["Rohlik listed your orders in a way I can't read: try again later",
                "if it keeps failing, report the message"]
    if stage == "sync":
        return ["Rohlik refused to list your orders or cart: try again in a few minutes",
                "if it keeps failing, check the Rohlik website is up"]
    if stage == "details":
        return ["Rohlik didn't return product details: try again in a few minutes"]
    return ["try again in a few minutes"]


def _error_md(stage: str, message: str, wrote: bool, actions: list[str]) -> str:
    lines = [f"**Failed at stage `{stage}`.** {message}",
             "The cart was changed before the failure; the next run finishes that safely." if wrote
             else "The cart was not changed.", "", "What you can do:"]
    lines.extend(f"- {a}" for a in actions)
    return "\n".join(lines)


def error_envelope(stage: str, message: str, wrote: bool, actions: list[str]) -> Envelope:
    return Envelope("error", _error_md(stage, message, wrote, actions),
                    {"stage": stage, "wrote": bool(wrote), "actions": actions}, [])


# ------------------------------------------------------------------------------------------ helpers
def _write_text(path: Path, text: str) -> None:
    """Write a file whole (a temporary file, then a rename), so a reader never sees half of it."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        tmp.replace(path)
    except OSError as exc:
        raise StageError("summary", f"couldn't write {path.name}: {exc}",
                         [f"check that the data directory can be written: {path.parent}", "then run again"]) from exc


def _waiting_skus(conn, upcoming: list, cart: Cart, e: date) -> list[int]:
    ids = {r[0] for r in conn.execute("SELECT DISTINCT sku FROM order_lines ORDER BY sku")}
    ids |= {r[0] for r in conn.execute("SELECT DISTINCT sku FROM run_upcoming_orders ORDER BY sku")}
    for u in upcoming:
        ids |= set(u.amounts)
    ids |= set(cart.lines)
    waiting = {r[0] for r in conn.execute("SELECT sku FROM skus WHERE assigned_by IS NULL ORDER BY sku")}
    return sorted(ids & waiting)


def _needs_tags_listing(conn, skus: list[int], details: dict[int, dict], e: date) -> list[dict]:
    """§7.1 step 6: each SKU waiting for tags, with what it was bought together with in at least 2 orders (at most
    8, most frequent first: product tags, and untagged products by name, so the first run has co-purchases too) and
    its order-line texts when they differ from the latest one."""
    from .sizes import differing_texts, parse
    out = []
    for sku in skus:
        s = conn.execute("SELECT * FROM skus WHERE sku = ?", (sku,)).fetchone()
        stats = conn.execute("SELECT COUNT(*), MAX(o.delivery_date) FROM order_lines l JOIN orders o "
                             "ON o.order_id = l.order_id WHERE l.sku = ?", (sku,)).fetchone()
        together = conn.execute(
            "SELECT COALESCE(t.name, s2.name) AS label, COUNT(DISTINCT l2.order_id) AS n FROM order_lines l1 "
            "JOIN order_lines l2 ON l2.order_id = l1.order_id AND l2.sku <> l1.sku JOIN skus s2 ON s2.sku = l2.sku "
            "LEFT JOIN product_tags t ON t.tag_id = s2.product_tag_id WHERE l1.sku = ? AND s2.ignored = 0 "
            "GROUP BY COALESCE(t.tag_id, 'sku:' || s2.sku) HAVING n >= ? ORDER BY n DESC, label LIMIT ?",
            (sku, BOUGHT_WITH_MIN_ORDERS, BOUGHT_WITH_MAX)).fetchall()
        texts = [r[0] for r in conn.execute("SELECT textual_amount FROM order_lines WHERE sku = ? ORDER BY order_id",
                                            (sku,))]
        parsed = parse(s["textual_amount"])
        item = {"sku": sku, "name": s["name"], "text": s["textual_amount"], "weighted": bool(s["weighted"]),
                "unit": parsed[1] if parsed else None, "size": parsed[0] if parsed else None,
                "times_bought": stats[0], "last_bought": stats[1],
                "bought_with": [f"{r['label']} ({r['n']}x)" for r in together]}
        line_texts = differing_texts(s["textual_amount"], texts, bool(s["weighted"]))
        if line_texts:
            item["line_texts"] = line_texts
        if parsed is None:
            prices = (details.get(sku) or {}).get("prices") or {}
            price = prices.get("salePrice") or prices.get("originalPrice")
            unit_price = prices.get("unitPrice")
            if price and unit_price:
                item["size_hint"] = f"~{price / unit_price:.3g} kg or l (price ÷ unitPrice)"
        out.append(item)
    return out


def _detail_ids(world: World, state: CycleState, e: date, now: datetime) -> set[int]:
    """SKUs of every tag with n >= 2 or managed long-life (§7.1 step 8)."""
    events = build_events(world, state, e, {})
    ids: set[int] = set()
    for tag in world.tags.values():
        if not tag.skus:
            continue
        td = build_tag_data(world, tag, events.get(tag.tag_id, []), e, e, 0.0, {}, {}, now)
        if td.n >= GATE_SUGGEST or td.managed:
            ids |= set(tag.skus)
    return ids


def _apply_texts(world: World, details: dict[int, dict]) -> None:
    for sku, d in details.items():
        info = world.skus.get(sku)
        if info is None:
            continue
        if d.get("textualAmount"):
            info.text = str(d["textualAmount"])
        if d.get("name"):
            info.name = str(d["name"])


def _delivery_date(client: RohlikClient, date_opt: date | None, e: date, report: RunReport) -> tuple[date, str]:
    """§6.5: `--date`, else the earliest date with a free slot in days 0..3, each day read on its own.

    A day whose read fails (§4.4) is skipped, not the end of the search: late in the day, day "0" answered with the
    tool's usage hint and no `availabilityDays` (§4.5). Only a transport failure that outlasts its
    retries ends the search, and the days it leaves unread count as unread. Without a free slot: today + 1, with a
    warning that says whether every day was read and none was free, or how many days couldn't be read."""
    if date_opt is not None:
        return date_opt, "your date"
    unread = 0
    for day in range(SLOT_DAYS):
        try:
            data = client.get_timeslots(day)
        except ToolFailure:
            unread += 1
            continue
        except TransportError:
            unread += SLOT_DAYS - day
            break
        found = _first_slot_date(data)
        if found is not None:
            return found, "earliest slot"
    if unread:
        report.warnings.append(f"couldn't read the delivery slots for {unread} of the next {SLOT_DAYS} days")
    else:
        report.warnings.append(f"no free delivery slot in the next {SLOT_DAYS} days")
    return e + timedelta(days=1), "tomorrow, no slot found"


def _first_slot_date(data: dict) -> date | None:
    days = data.get("availabilityDays")
    if isinstance(days, dict):
        days = [days]
    best = None
    for d in days or []:
        if not isinstance(d, dict):
            continue
        slots = d.get("slots") or {}
        groups = slots.values() if isinstance(slots, dict) else [slots]
        if any(isinstance(s, dict) and s.get("slotId") for g in groups for s in (g if isinstance(g, list) else [g])):
            day = parse_date_loose(d.get("date"))
            if day and (best is None or day < best):
                best = day
    return best


def _recover(conn, world: World, state: CycleState, cart: dict[int, float], report: RunReport) -> None:
    non_dry = [r for r in world.runs if r.non_dry]
    if not non_dry or non_dry[-1].state != "writing":
        return
    crashed = non_dry[-1]
    closing = next((c.closing for c in state.closed if crashed in c.runs), None)
    if closing is not None:
        x = {s: closing.qty(s) for s in closing.units}
    else:
        x = dict(cart)
    values = recovery_values(crashed, x)
    with transaction(conn):
        for sku, (after, owned) in values.items():
            conn.execute("UPDATE run_cart SET after_qty = ?, agent_owned = ? WHERE run_id = ? AND sku = ?",
                         (after, owned, crashed.run_id, sku))
        conn.execute("UPDATE runs SET state = 'done', recovered = 1 WHERE run_id = ?", (crashed.run_id,))
    crashed.state = "done"
    crashed.recovered = True
    crashed.owned = {s: v[1] for s, v in values.items()}
    crashed.after = {s: v[0] for s, v in values.items()}
    for s, (after, owned) in values.items():
        row = crashed.cart[s]
        row.after_qty, row.agent_owned = after, owned
    report.recovered_run = crashed.run_id


def _cart_rows(report: RunReport) -> dict[int, tuple[float, float, float | None]]:
    """run_cart snapshot: every SKU in the cart, planned, or agent-owned by the previous run."""
    own = report.own
    cart = report.cart.quantities()
    skus = set(cart) | {r.sku for r in report.plan.rows} | {s for s, q in (own.last.owned if own.last else {}).items()
                                                            if q > EPS}
    out = {}
    for sku in sorted(skus):
        out[sku] = (cart.get(sku, 0.0), own.user_owned.get(sku, cart.get(sku, 0.0)), report.targets.get(sku))
    return out


def _insert_cart_rows(conn, run_id: str, report: RunReport, dry: bool) -> None:
    for sku, (before, user, target) in _cart_rows(report).items():
        if dry:
            conn.execute("INSERT INTO run_cart(run_id, sku, before_qty, user_owned_before, target_qty, after_qty, "
                         "agent_owned) VALUES (?,?,?,?,?,?,?)", (run_id, sku, before, user, target, before,
                                                                 max(0.0, before - user)))
        else:
            conn.execute("INSERT INTO run_cart(run_id, sku, before_qty, user_owned_before, target_qty) "
                         "VALUES (?,?,?,?,?)", (run_id, sku, before, user, target))


def _owned_after(after: float, before: float, user: float, target: float | None) -> float:
    tgt = before if target is None else target
    return clamp(after - user, 0.0, max(0.0, max(tgt, before) - user))


def _write_protocol(env: Env, conn, report: RunReport, options: str, upcoming: list) -> None:
    client = env.client
    world, plan, own = report.world, report.plan, report.own
    rows = _cart_rows(report)
    cart1 = report.cart
    state = report.state
    # 1. the write-ahead commit
    with transaction(conn):
        _insert_run(conn, report.run_id, report.now, "writing", report.t, report.cadence, options)
        _insert_upcoming(conn, report.run_id, upcoming)
        _insert_plan(conn, report.run_id, plan)
        _insert_cart_rows(conn, report.run_id, report, dry=False)
        for sku, prop in sorted(own.new_edits.items()):
            conn.execute("INSERT INTO run_edits(run_id, sku, proposal_run) VALUES (?,?,?)",
                         (report.run_id, sku, prop.run_id))
        logged = _insert_calls(conn, report.run_id, client, 0)
    intended = {s for s, (before, _, target) in rows.items() if target is not None and abs(target - before) > EPS}
    if not intended:
        report.final_cart = cart1
        return
    # 2. re-read once; a tag with any SKU changed since planning is skipped whole and locked
    cart2 = parse_cart(client.get_cart())
    by_tag: dict[str, set[int]] = {}
    for s in intended:
        tag = world.tag_of(s)
        by_tag.setdefault(tag.tag_id if tag else f"#{s}", set()).add(s)
    skipped: set[int] = set()
    changed_edits: dict[int, str] = {}
    this_run = RunRecord(report.run_id, int(report.run_id[1:7]), report.now, None, "writing", False, report.t, None,
                         {}, plan={r.sku: _plan_rec(r) for r in plan.rows})
    cycle_runs = state.current.runs + [this_run]
    for tag_id in sorted(by_tag):
        tag = world.tags.get(tag_id)
        skus = set(tag.skus) if tag else set(by_tag[tag_id])
        skus |= {s for s in set(cart1.lines) | set(cart2.lines) if world.tag_of(s) is tag and tag is not None}
        changed = sorted(s for s in skus if abs(cart2.qty(s) - cart1.qty(s)) > EPS)
        if changed:
            skipped |= {s for s in skus if s in rows}
            for s in changed:
                if s not in own.new_edits:
                    changed_edits[s] = proposing_run(cycle_runs, s, this_run).run_id
            report.skipped_tags.append(tag.name if tag else str(tag_id))
    if skipped or changed_edits:
        with transaction(conn):
            for s in sorted(skipped):
                before, user, _ = rows[s]
                conn.execute("UPDATE run_cart SET after_qty = ?, agent_owned = ? WHERE run_id = ? AND sku = ?",
                             (before, max(0.0, before - user), report.run_id, s))
            for s, prop in sorted(changed_edits.items()):
                conn.execute("INSERT OR IGNORE INTO run_edits(run_id, sku, proposal_run) VALUES (?,?,?)",
                             (report.run_id, s, prop))
    # 3./4. writes
    order = sorted(intended - skipped, key=lambda s: (_tag_key(world, s), s))
    attempted: set[int] = set()
    stop = False
    for s in order:
        if stop:
            break
        before, user, target = rows[s]
        current = cart2.qty(s)
        if abs(target - current) <= EPS:
            continue
        attempted.add(s)
        tool = "update_cart_item" if s in cart2.lines else "add_items_to_cart"
        ok, fatal = _write_one(client, s, int(round(target)), s in cart2.lines)
        if ok:
            _confirm(conn, report.run_id, s, target, before, user)
            report.writes.append(WriteRecord(s, tool, before, target, "confirmed"))
        else:
            report.writes.append(WriteRecord(s, tool, before, target, "failed"))
            report.warnings.append(f"could not set {world.skus[s].name} to {target:g}")
            stop = fatal
    # 5. final re-read; rows without a stored after_qty get one at finalisation
    report.final_cart = parse_cart(client.get_cart())
    report.attempted = attempted


def _plan_rec(r):
    from .world import PlanRowRec
    return PlanRowRec(r.sku, r.reason, r.normal_units, r.stockup_units, r.sale_until, r.size, r.size_unit,
                      r.fallback_from)


def _tag_key(world: World, sku: int) -> tuple:
    tag = world.tag_of(sku)
    return (tag.name, tag.tag_id) if tag else ("", "")


def _write_one(client: RohlikClient, sku: int, target: int, present: bool) -> tuple[bool, bool]:
    """One SKU's write with the §6.15 error handling. Returns (confirmed, stop_writing).

    The first write and the retry use add_items_to_cart only for a line known to be absent, because
    update_cart_item can't create one (V4). When the re-read after an error fails, the line's state is unknown,
    and add_items_to_cart would add on top of an existing line, so the retry is the idempotent update_cart_item."""
    try:
        if present:
            client.update_item(sku, target)
        else:
            client.add_item(sku, target)
        return True, False
    except AuthRequired:
        return _reached(_reread(client, sku, target)), True
    except (ToolFailure, TransportError):
        seen = _reread(client, sku, target)
        if _reached(seen):
            return True, False
        # §6.15 step 4: the retry's error decides whether writing continues
        try:
            if seen is None or seen[1]:
                client.update_item(sku, target)
            else:
                client.add_item(sku, target)
            return True, False
        except AuthRequired:
            return False, True
        except ToolFailure:
            return _reached(_reread(client, sku, target)), False
        except TransportError:
            return _reached(_reread(client, sku, target)), True


def _reread(client: RohlikClient, sku: int, target: int) -> tuple[bool, bool] | None:
    """(s is at its target, s has a cart line) from a fresh cart read; None if the read fails."""
    try:
        cart = parse_cart(client.get_cart())
    except (ToolFailure, TransportError, AuthRequired):
        return None
    return abs(cart.qty(sku) - target) <= EPS, sku in cart.lines


def _reached(seen: tuple[bool, bool] | None) -> bool:
    return seen is not None and seen[0]


def _confirm(conn, run_id: str, sku: int, target: float, before: float, user: float) -> None:
    with transaction(conn):
        conn.execute("UPDATE run_cart SET after_qty = ?, agent_owned = ? WHERE run_id = ? AND sku = ?",
                     (target, _owned_after(target, before, user, target), run_id, sku))


def _checkout_link(client: RohlikClient, report: RunReport) -> str | None:
    """§7.1 step 12: a failed link read is only a warning; the run still finishes."""
    try:
        return client.get_checkout_link()
    except (ToolFailure, TransportError):
        report.warnings.append("the checkout link couldn't be read — open the Rohlik app or website to check out")
        return None


def _finalise(conn, report: RunReport, md: str, client: RohlikClient, finished: datetime) -> None:
    attempted = report.attempted
    final = report.final_cart or report.cart
    with transaction(conn):
        rows = conn.execute("SELECT * FROM run_cart WHERE run_id = ? ORDER BY sku", (report.run_id,)).fetchall()
        for r in rows:
            if r["after_qty"] is not None and r["agent_owned"] is not None:
                continue
            after = final.qty(r["sku"]) if r["sku"] in attempted else r["before_qty"]
            owned = _owned_after(after, r["before_qty"], r["user_owned_before"], r["target_qty"])
            conn.execute("UPDATE run_cart SET after_qty = ?, agent_owned = ? WHERE run_id = ? AND sku = ?",
                         (after, owned, report.run_id, r["sku"]))
        missing = conn.execute("SELECT COUNT(*) FROM run_cart WHERE run_id = ? AND (after_qty IS NULL OR "
                               "agent_owned IS NULL)", (report.run_id,)).fetchone()[0]
        if missing:
            raise AssertionError("a done run must have after_qty and agent_owned on every cart row")
        _store_modes(conn, report.plan, report.now)
        set_meta(conn, "preferred_check", None)  # §8.1: a non-dry run that finishes normally ends the check
        conn.execute("UPDATE runs SET state = 'done', finished_at = ?, summary_md = ? WHERE run_id = ?",
                     (iso(finished), md, report.run_id))
        _insert_calls(conn, report.run_id, client, 0)
