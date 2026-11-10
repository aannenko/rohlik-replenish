"""status (§9): history depth, cadence, last run, owned lines, locks, paused tags, dropped types, live precision and
recall over the last 5 matched orders. Offline: it reads only the database."""

from __future__ import annotations

from datetime import datetime

from .constants import EPS
from .cycles import build_placed, close_ownership, resolve_cycles
from .planner import cadence_of
from .replay import replay
from .timeutil import fmt_local, local_date
from .world import RunRecord, load_world


def status(conn, now: datetime) -> tuple[str, dict]:
    world = load_world(conn)
    e = local_date(now)
    current = RunRecord("r999999-status", 999999, now, None, "current", False, None, None, {})
    placed = build_placed(world, [], current, e)
    state = resolve_cycles(world, placed)
    close_ownership(world, state)
    rep = replay(world, state)
    hist = sorted(o.delivery_date for o in world.orders.values() if o.delivery_date < e)
    cadence = cadence_of(hist, world.cadence_override)
    lines = ["# Status", ""]
    lines.append(f"- history: {len(hist)} delivered orders" + (f", {hist[0]} to {hist[-1]}" if hist else ""))
    lines.append(f"- cadence: every {cadence:g} days" + (" (your setting)" if world.cadence_override else ""))
    last = world.runs[-1] if world.runs else None
    if last:
        fin = fmt_local(last.finished_at) if last.finished_at else "—"
        lines.append(f"- last run: {last.run_id} · {last.state}{' (recovered)' if last.recovered else ''} · started "
                     f"{fmt_local(last.started_at)} · finished {fin}")
    else:
        lines.append("- last run: none")
    owner = state.current.last
    owned = {s: q for s, q in (owner.owned if owner else {}).items() if q > EPS}
    lines.append("- lines I own in the cart: " + (", ".join(
        f"{world.skus[s].name} ×{q:g}" for s, q in sorted(owned.items())) if owned else "none"))
    ids = state.current.run_ids()
    locks = sorted({world.tag_of(s).name for r in state.current.runs for s, p in r.edits.items()
                    if p in ids and world.tag_of(s)})
    lines.append("- kept as you edited (this cycle): " + (", ".join(locks) if locks else "none"))
    paused = sorted(world.tags[t].name for t in rep.paused if t in world.tags)
    lines.append("- paused: " + (", ".join(paused) if paused else "none"))
    dropped = sorted(f"{world.skus[s].name} ({world.tags[t].name})" for (t, s) in rep.dropped if t in world.tags)
    lines.append("- dropped types: " + (", ".join(dropped) if dropped else "none"))
    # `overdue` rows are stored by every run (§6.11 rule 11): the latest planned run's are the current reminders
    planned = next((r for r in reversed(world.runs) if r.state in ("dry", "done", "writing") and r.plan), None)
    overdue = sorted({world.tag_of(s).name for s, row in (planned.plan if planned else {}).items()
                      if row.reason == "overdue" and world.tag_of(s)})
    lines.append("- overdue (check your stock): " + (", ".join(overdue) if overdue else "none"))
    # §6.13: a solo tag buying without its preferred product, or unavailable for want of it, as the latest plan had it
    fallbacks = []
    for s, row in sorted((planned.plan if planned else {}).items()):
        tag = world.tag_of(s)
        if row.fallback_from is None or tag is None or row.fallback_from not in world.skus:
            continue
        pref = world.skus[row.fallback_from].name
        fallbacks.append(f"{tag.name} ({pref} unavailable" + (f", buying {world.skus[s].name}"
                                                                if row.reason in ("due", "stockup") else "") + ")")
    lines.append("- solo tags without their preferred product: " + (", ".join(fallbacks) if fallbacks else "none"))
    matched = [p for p in state.placed if p.cycle is not None and not p.cancelled and rep.added.get(p.order_id)][-5:]
    precs, recs = [], []
    for p in matched:
        added, got = rep.added[p.order_id], rep.contained.get(p.order_id, set())
        precs.append(len(added & got) / len(added))
        if got:
            recs.append(len(added & got) / len(got))
    data = {"orders": len(hist), "cadence": cadence, "last_run": last.run_id if last else None,
            "owned": {str(s): q for s, q in sorted(owned.items())}, "locked": locks, "paused": paused,
            "dropped": dropped, "overdue": overdue, "solo_fallbacks": fallbacks, "matched_orders": len(matched)}
    if matched:
        prec = sum(precs) / len(precs)
        rec = sum(recs) / len(recs) if recs else 0.0
        lines.append(f"- last {len(matched)} orders filled by me: {prec:.0%} of my lines kept; {rec:.0%} of the tags "
                     f"you ordered had been added by me")
        data.update(live_precision=round(prec, 4), live_recall=round(rec, 4))
    else:
        lines.append("- no order filled by me yet")
    return "\n".join(lines) + "\n", data
