"""Command line (§9). Every command prints one JSON envelope to stdout, in UTF-8 (§8.3)."""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

from .db import connect
from .oslock import DataLock, LockBusy
from .paths import AuthFile, ConfigError, account_db_path, data_dir, load_config
from .runner import Envelope, Env, error_envelope, run
from .tagsvc import (TagError, prefs_cadence, prefs_never, tags_apply, tags_list, tags_merge, tags_rename, tags_set,
                     tags_storage_set)
from .timeutil import parse_iso, utcnow


class _ArgError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # an envelope, not argparse's usage text and exit
        raise _ArgError(message)


def _utf8() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
        else:  # pragma: no cover
            setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace"))


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--fixtures", metavar="DIR", help="test only: recorded responses replace the MCP client")
    common.add_argument("--now", metavar="ISO", help="test only (with --fixtures, or in backtest)")
    p = _Parser(prog="replenish.py", description="Rohlik replenishment engine")
    sub = p.add_subparsers(dest="command", required=True, parser_class=_Parser)
    sub.add_parser("login", parents=[common], help="interactive login")
    r = sub.add_parser("run", parents=[common], help="one run")
    r.add_argument("--date", metavar="YYYY-MM-DD")
    r.add_argument("--dry-run", action="store_true")
    t = sub.add_parser("tags", parents=[common], help="product and storage tags")
    ts = t.add_subparsers(dest="tags_cmd", required=True, parser_class=_Parser)
    src = argparse.ArgumentParser(add_help=False)
    src.add_argument("--source", choices=("agent", "user"), default="agent")
    tl = ts.add_parser("list", parents=[common])
    tl.add_argument("--tag")
    ta = ts.add_parser("apply", parents=[common, src])
    ta.add_argument("file")
    tr = ts.add_parser("rename", parents=[common, src])
    tr.add_argument("tag")
    tr.add_argument("new_name")
    tm = ts.add_parser("merge", parents=[common, src])
    tm.add_argument("from_tag")
    tm.add_argument("into_tag")
    tset = ts.add_parser("set", parents=[common, src])
    tset.add_argument("tag")
    tset.add_argument("--policy", choices=("cheapest", "assorted", "solo", "variety"))
    tset.add_argument("--preferred", metavar="SKU", help="the product a solo tag buys (makes the tag solo)")
    tset.add_argument("--stockup-mode", choices=("always", "on_sale"))
    tset.add_argument("--note")
    tset.add_argument("--base-unit", choices=("kg", "l", "pcs", "m"))
    tst = ts.add_parser("storage-set", parents=[common, src])
    tst.add_argument("name")
    tst.add_argument("--stockup", choices=("yes", "no"), required=True)
    pr = sub.add_parser("prefs", parents=[common], help="standing preferences")
    ps = pr.add_subparsers(dest="prefs_cmd", required=True, parser_class=_Parser)
    pn = ps.add_parser("never", parents=[common])
    pn.add_argument("action", choices=("add", "remove"))
    pn.add_argument("tag")
    pc = ps.add_parser("cadence", parents=[common])
    pc.add_argument("days")
    b = sub.add_parser("backtest", parents=[common], help="backtest on your history or a CSV dataset")
    b.add_argument("--csv", metavar="DIR")
    b.add_argument("--raw-units", action="store_true")
    sub.add_parser("status", parents=[common], help="history, ownership, locks, pauses")
    return p


def main(argv: list[str] | None = None) -> int:
    _utf8()
    logging.basicConfig(level=logging.CRITICAL)  # the SDK logs flow errors; the envelope reports them
    try:
        args = build_parser().parse_args(argv)
    except _ArgError as exc:
        env = error_envelope("arguments", str(exc), False, ["check the command line (see README.md)"])
        print(env.to_json())
        return env.exit_code
    env = _dispatch_safely(args)
    print(env.to_json())
    sys.stdout.flush()
    return env.exit_code


def _dispatch_safely(args) -> Envelope:
    """§8.3: every command prints exactly one envelope, also when it fails unexpectedly (stage `internal`)."""
    try:
        return dispatch(args)
    except Exception as exc:  # noqa: BLE001
        return error_envelope("internal", f"{type(exc).__name__}: {exc}", False,
                              ["this is a fault in the skill: run the command again, and if it keeps failing, report "
                               "the message"])


def _now(args) -> datetime:
    if args.now:
        if not (args.fixtures or args.command == "backtest"):
            raise _ArgError("--now is only allowed with --fixtures or for backtest")
        return parse_iso(args.now)
    return utcnow()


def dispatch(args) -> Envelope:
    command = args.command
    try:
        now = _now(args)
    except (_ArgError, ValueError) as exc:
        return error_envelope("arguments", str(exc), False, ["check --now (ISO 8601, e.g. 2026-10-09T08:00:00+02:00)"])
    directory = data_dir()
    lock = DataLock(directory)
    try:
        lock.acquire()
    except LockBusy:
        return error_envelope("lock", "another command of this skill is running", False,
                              ["another run is in progress: wait for it to finish"])
    try:
        if command == "login":
            return _login(directory)
        if command == "run":
            return _run(args, directory, now)
        if command == "backtest":
            return _backtest(args, directory, now)
        return _offline(args, directory, now)
    finally:
        lock.release()


def _login(directory: Path) -> Envelope:
    from .mcp_transport import SecretStore, TokenStore, login
    from .rohlik import KEYRING_ACTIONS, KeyringFailure

    store = TokenStore(SecretStore(), AuthFile(directory))
    try:
        facts = login(store)
    except KeyringFailure as exc:
        return error_envelope("login", f"login did not complete: {exc}", False, KEYRING_ACTIONS)
    except Exception as exc:  # noqa: BLE001
        return error_envelope("login", f"login did not complete: {exc}", False,
                              ["run `login` again and finish it in the browser within 5 minutes",
                               "if the browser didn't open, copy the URL printed on stderr"])
    md = (f"**Logged in to Rohlik.** Session refresh: "
          f"{'yes' if facts['refresh_token_issued'] else 'no (log in again when a run asks)'}.")
    return Envelope("ok", md, facts, [])


def _run(args, directory: Path, now: datetime) -> Envelope:
    from .rohlik import AuthRequired, RohlikClient, TransportError

    try:
        config = load_config(directory)
    except ConfigError as exc:
        return error_envelope("config", str(exc), False, ["fix or delete config.toml in the data directory"])
    try:
        date_opt = date.fromisoformat(args.date) if args.date else None
    except ValueError:
        return error_envelope("options", f"--date '{args.date}' is not YYYY-MM-DD", False, ["use e.g. --date 2026-10-09"])
    auth = AuthFile(directory)

    def open_db(account: str):
        path = account_db_path(directory, account)
        path.parent.mkdir(parents=True, exist_ok=True)
        return connect(str(path), now)

    if args.fixtures:
        from .fake import FakeRohlik
        from .runner import SessionInfo

        transport = FakeRohlik.from_dir(Path(args.fixtures))
        session = SessionInfo()
    else:
        from .mcp_transport import McpTransport, SecretStore, TokenStore
        from .rohlik import KEYRING_ACTIONS, KeyringFailure

        store = TokenStore(SecretStore(), auth)
        try:
            if not store.has_session():
                return Envelope("auth_required", "**Login needed.** There is no valid Rohlik session. Run `login`, "
                                "finish it in the browser, then run again.", {"wrote": False}, [])
        except KeyringFailure as exc:
            return error_envelope("auth", str(exc), False, KEYRING_ACTIONS)
        transport = McpTransport(store)
        session = store
        try:
            names = {t["name"] for t in transport.list_tools()}
        except AuthRequired:
            transport.close()
            return Envelope("auth_required", "**Login needed.** The Rohlik session has expired. Run `login`, finish "
                            "it in the browser, then run again.", {"wrote": False}, [])
        except KeyringFailure as exc:
            transport.close()
            return error_envelope("auth", str(exc), False, KEYRING_ACTIONS)
        except TransportError as exc:
            transport.close()
            return error_envelope("connect", f"could not reach Rohlik: {exc}", False,
                                  ["Rohlik didn't answer: try again in a few minutes"])
        from .constants import ALLOWED_TOOLS
        missing = sorted(ALLOWED_TOOLS - names)
        if missing:
            transport.close()
            return error_envelope("tools", f"Rohlik no longer offers: {', '.join(missing)}", False,
                                  ["the skill needs an update for Rohlik's changed tools"])
    client = RohlikClient(transport)
    env = Env(directory, client, now, config, auth, session, open_db, fixtures=bool(args.fixtures),
              clock=None if args.fixtures else utcnow)
    try:
        return run(env, date_opt, args.dry_run)
    finally:
        transport.close()


def _account_conn(directory: Path, now: datetime):
    account = AuthFile(directory).load().get("last_account_id")
    if not account:
        return None
    path = account_db_path(directory, account)
    if not path.exists():
        return None
    return connect(str(path), now)


def _backtest(args, directory: Path, now: datetime) -> Envelope:
    from .backtest import interpretation, load_csv_world, pct, run_backtest
    from .world import load_world

    notes = []
    if args.csv:
        try:
            world, notes = load_csv_world(Path(args.csv), raw_units=args.raw_units)
        except (OSError, KeyError, ValueError, TagError) as exc:
            return error_envelope("backtest", f"could not load the CSV dataset: {exc}", False,
                                  ["point --csv at a folder with purchases.csv, products.csv and orders.csv"])
    else:
        conn = _account_conn(directory, now)
        if conn is None:
            return error_envelope("backtest", "no history yet", False, ["run `run` first, or pass --csv DIR"])
        world = load_world(conn)
        if args.raw_units:
            for info in world.skus.values():
                if info.tag_id:
                    info.upp, info.upp_unit = 1.0, world.tags[info.tag_id].base_unit
    if args.now:
        cut = parse_iso(args.now).date()
        world.orders = {k: o for k, o in world.orders.items() if o.delivery_date <= cut}
    res = run_backtest(world, raw_units=args.raw_units)
    res.notes = notes + res.notes
    d = res.as_dict()
    if res.evaluated:
        md = (f"**Backtest** ({res.evaluated} orders{', raw sale units' if args.raw_units else ''}): "
              f"{res.proposed_per_order:.1f} tags proposed per order · precision {pct(res.precision_same)} same "
              f"order, {pct(res.precision_next)} this or next · recall {res.recall_lines:.0%} of lines, "
              f"{res.recall_spend:.0%} of spend · {res.suggestions_per_order:.1f} suggestions per order.\n\n"
              f"In words: {interpretation(res)}\n")
    else:
        md = "**Backtest:** more history is needed (the first order evaluated is 183 days after the first one).\n"
    return Envelope("ok", md, d, notes)


def _offline(args, directory: Path, now: datetime) -> Envelope:
    command = args.command
    conn = _account_conn(directory, now)
    if conn is None:
        return error_envelope(command, "no account data yet", False, ["run `run` first (after `login`)"])
    try:
        if command == "status":
            from .status import status

            md, data = status(conn, now)
            return Envelope("ok", md, data, [])
        if command == "prefs":
            if args.prefs_cmd == "never":
                res = prefs_never(conn, args.action, args.tag, now)
            else:
                res = prefs_cadence(conn, args.days)
            return Envelope("ok", "\n".join(res.messages) + "\n", {}, res.warnings)
        sub = args.tags_cmd
        if sub == "list":
            return Envelope("ok", tags_list(conn, args.tag), {}, [])
        if sub == "apply":
            try:
                entries = json.loads(Path(args.file).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                return error_envelope("tags", f"could not read {args.file}: {exc}", False,
                                      ["write a JSON array of entries (see reference/tagging.md)"])
            res = tags_apply(conn, entries, args.source, now)
        elif sub == "rename":
            res = tags_rename(conn, args.tag, args.new_name, args.source, now)
        elif sub == "merge":
            res = tags_merge(conn, args.from_tag, args.into_tag, args.source, now)
        elif sub == "set":
            res = tags_set(conn, args.tag, policy=args.policy, stockup_mode=args.stockup_mode, note=args.note,
                           base_unit=args.base_unit, preferred=args.preferred, source=args.source, now=now)
        else:
            res = tags_storage_set(conn, args.name, args.stockup == "yes", now)
        return Envelope("ok", "\n".join(res.messages) + "\n", res.data, res.warnings)
    except TagError as exc:
        return Envelope("error", "**Nothing was changed.**\n\n" + "\n".join(f"- {e}" for e in exc.errors) + "\n",
                        {"stage": command, "wrote": False, "actions": ["fix the listed problems and run it again"],
                         "errors": exc.errors}, [])
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
