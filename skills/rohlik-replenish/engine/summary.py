"""Summary (§8.1), setup report (§8.2) and the `data` JSON of the run envelope (§8.3)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from .constants import EPS, GATE_ADD, GATE_SUGGEST, REMINDER_RUNS, SUMMARY_NAMES_MAX, WINDOW_LONG
from .numerics import tidy
from .preferred import fallback_since, showed_fallback
from .sizes import differing_texts, size_of
from .timeutil import fmt_day, local_date

REASON_TEXT = {
    "wait_sale": "running low — waiting for a sale",
    "basket_limit": "basket limit reached",
}


def _name(report, sku: int) -> str:
    return report.world.skus[sku].name if sku in report.world.skus else f"#{sku}"


def _num(x: float) -> str:
    return f"{x:g}" if abs(x - round(x)) > 1e-9 else str(int(round(x)))


# ------------------------------------------------------------------------------------------ lines
def _why(tp, report) -> str:
    td = tp.td
    parts = []
    first = tp.rows[0]
    z = first.size or 0.0
    if td and td.rate and z > EPS:
        per_day = td.rate / z
        if per_day <= 1 + 1e-9:
            days = 1 / per_day if per_day > EPS else 0
            parts.append(f"~1 per {round(days)} days" if days >= 1.5 else "~1 per day")
        else:
            parts.append(f"~{per_day:.1f} per day")
        proj = td.projection
        if proj is not None and report.t:
            parts.append(f"~{proj.on_hand_t / z:.1f} left on {fmt_day(report.t)}")
            added = sum((r.normal_units + r.stockup_units) * (r.size or 0) for r in tp.rows)
            total = proj.on_hand_t + proj.incoming_after_t + added
            lasts = report.t + timedelta(days=int(total / td.rate + 1e-9))
            parts.append(f"lasts to {fmt_day(lasts)}")
    if tp.kind == "always" and td and td.usual_batch and z > EPS and tp.fallback_from is None:
        parts.append(f"your usual {_num(round(td.usual_batch / z, 1))}")
    return " · ".join(parts)


def _which(tp, report) -> str:
    n = len(tp.cands) if tp.tag.policy != "variety" else len(tp.ordered)
    skus = [r.sku for r in tp.rows]
    out = []
    if tp.fallback_from is not None:
        why = "comes in a bigger pack than needed" if tp.fallback_too_big else "unavailable"
        out.append(f"{_pname(report, tp.fallback_from)} {why} — {_pname(report, skus[0])} instead")
    elif tp.tag.policy == "cheapest" and n > 1:
        out.append(f"cheapest of {n} options")
    elif tp.tag.policy == "assorted":
        out.append(f"the {len(skus)} cheapest of {n} options" if n > 1 else "")
    elif tp.tag.policy == "variety":
        if len(tp.rows) == 1:
            out.append("your recent mix")
        else:
            out.append(", ".join(f"{_pname(report, r.sku)} ×{r.normal_units + r.stockup_units}" for r in tp.rows)
                       + " — your recent mix")
    if tp.alloc and tp.alloc.capped_usual:
        out.append("capped at your usual quantity")
    if tp.alloc and tp.alloc.capped_basket:
        out.append("capped at the basket limit")
    return " · ".join(x for x in out if x)


def _qty_label(tp, report) -> str:
    if len(tp.rows) == 1:
        r = tp.rows[0]
        return f"**{_name(report, r.sku)}** ×{r.normal_units + r.stockup_units}"
    return f"**{tp.tag.name}** ×{sum(r.normal_units + r.stockup_units for r in tp.rows)}"


def _would_add_lines(report) -> list[str]:
    """Dry runs only (the cart doesn't show them): product · quantity, one line each, top-ups marked (§8.1)."""
    out = []
    for tp in report.plan.plans:
        if tp.category != "line":
            continue
        for r in tp.rows:
            mark = f" (incl. {r.stockup_units} top-up)" if r.stockup_units else ""
            out.append(f"- {_pname(report, r.sku)} ×{r.normal_units + r.stockup_units}{mark}")
    return out


def _topup_lines(report) -> list[str]:
    """One line per `on_sale` tag topped up: product · top-up quantity · Rohlik's discount · until <date> (§8.1)."""
    out = []
    for tp in report.plan.plans:
        if tp.category != "line" or not tp.alloc or tp.alloc.stockup_total == 0:
            continue
        rows = [r for r in tp.rows if r.stockup_units]
        product = _pname(report, rows[0].sku) if len(rows) == 1 else tp.tag.name
        disc = max((tp.cands[r.sku].live.discount or 0.0) for r in rows)
        ends = [r.sale_until for r in rows if r.sale_until]
        bits = [f"**{product}**", f"+{sum(r.stockup_units for r in rows)} top-up", f"−{round(disc * 100)} %",
                f"until {fmt_day(min(ends))}" if ends else "no end date"]
        if tp.budget_trimmed:
            bits.append("trimmed to the stock-up budget")
        out.append("- " + " · ".join(bits))
    return out


def _suggestion_lines(report) -> list[str]:
    out = []
    for tp in report.plan.plans:
        name = tp.tag.name
        if tp.category == "kept":
            out.append(f"- **{name}**: couldn't check today — kept as it was")
            continue
        if tp.category == "unavailable":
            shown = tp.fallback_from or tp.rep_sku  # a solo tag names its preferred product (§6.13)
            product = f" ({_pname(report, shown)})" if shown else ""
            out.append(f"- **{name}**{product}: unavailable: {tp.unavailable_reason}")
            continue
        if tp.category != "suggestion":
            continue
        if tp.reason == "overdue" and not _reminder_visible(report, tp.reminder_since):
            continue  # a reminder (§2): after 3 normally finished runs only `status` lists it
        if tp.reason == "size_unknown":
            notes = tp.td.unknown_notes if tp.td and tp.td.unknown_notes else [
                f"pack size unknown: {_pname(report, tp.rep_sku) if tp.rep_sku else name}"]
            text = "; ".join(sorted(set(notes)))
        elif tp.reason == "overdue":
            text = f"{tp.note} — check your stock"
        elif tp.reason == "budget_limit":
            text = f"{tp.note or 'on sale'} — skipped: stock-up budget reached"
        elif tp.reason == "suggest_gate":  # names the window, so it doesn't contradict a longer count (§6.11 rule 5)
            text = f"bought twice in {12 if tp.tag.long_life else 6} months"
        else:
            text = REASON_TEXT.get(tp.reason, tp.reason)
        product = f" ({_pname(report, tp.rep_sku)})" if tp.rep_sku and tp.reason not in ("size_unknown",) else ""
        out.append(f"- **{name}**{product}: {text}")
    return out


def _dated_visible(report, d: date | None, runs: int, since: datetime | None = None,
                   any_day: bool = False, counts=None) -> bool:
    """A line dated d (§2): shown only when d is after the day setup completed, by every run that starts on or after
    d, until `runs` non-dry runs that started on or after d have finished normally (`done` with `finished_at`; a
    recovered crash doesn't count); the last of them shows it too.

    A pause or a dropped type passes `since`, its moment m (the placement of the order with the second removal):
    the runs counted are those that started after m (§2, §6.11 rule 2), so a run earlier that day, which couldn't
    know, doesn't use up one of its showings. A run that starts exactly at m belongs after it, as in §6.6. As for
    dated lines, nothing whose day is on or before the setup day is shown, except with `any_day`: a solo tag's
    fallback reminder, whose moment is a run's start, never old history, so a streak that began in the setup run
    still shows (§6.13). `counts`, when given, says which of those runs count: a solo fallback reminder counts only
    the runs that showed it."""
    setup = report.world.setup_completed_at
    setup_day = local_date(setup) if setup else report.e
    if since is not None:
        if (local_date(since) <= setup_day and not any_day) or report.now < since:
            return False
        finished = sum(1 for r in report.world.runs if r.non_dry and r.state == "done" and r.finished_at is not None
                       and r.started_at >= since and (counts is None or counts(r)))
        return finished < runs
    if d is None:
        return False
    if not d > setup_day or report.e < d:
        return False
    finished = sum(1 for r in report.world.runs if r.non_dry and r.state == "done" and r.finished_at is not None
                   and local_date(r.started_at) >= d)
    return finished < runs


def _reminder_visible(report, d: date | None = None, since: datetime | None = None, any_day: bool = False,
                      counts=None) -> bool:
    """A reminder (§2): a paused tag, a dropped type, an `overdue` suggestion or a solo fallback, for 3 normally
    finished runs."""
    return _dated_visible(report, d, REMINDER_RUNS, since, any_day, counts)


def _notice_visible(report, d: date) -> bool:
    """A one-time notice (§2)."""
    return _dated_visible(report, d, 1)


def _reminder_lines(report) -> list[str]:
    """§8.1 section 4, "Reminders" (§2): paused tags, all-dropped `variety` tags and dropped types, the one-time
    notices, and `solo` tags buying without their preferred product. Each line keeps its own wording, so a paused
    tag still reads "Paused: …"."""
    rep = report.replay
    world = report.world
    out = []
    for tag_id, info in sorted(rep.paused.items(), key=lambda kv: world.tags[kv[0]].sort_key
                               if kv[0] in world.tags else (kv[0], "")):
        tag = world.tags.get(tag_id)
        if tag is not None and _reminder_visible(report, since=info.since):
            out.append(f"- Paused: **{tag.name}** — removed twice; buy it once, or ask me to add it, and I'll resume")
    for tp in report.plan.plans:  # variety tags whose types are all dropped
        if tp.category == "paused" and tp.tag.tag_id not in rep.paused:
            moments = [i.since for (t, _), i in rep.dropped.items() if t == tp.tag.tag_id]
            if moments and _reminder_visible(report, since=max(moments)):
                out.append(f"- **{tp.tag.name}**: all types dropped — buy one to resume")
    for (tag_id, sku), info in sorted(rep.dropped.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        tag = world.tags.get(tag_id)
        if tag is not None and _reminder_visible(report, since=info.since):
            out.append(f"- {_pname(report, sku)} dropped from {tag.name} — removed twice; buy it once and it's back")
    out.extend(_notices(report))
    out.extend(_solo_reminders(report))
    return out


def solo_fallback_text(report, tp) -> str:
    """§6.13: "eggs: Pohodová L unavailable (Vyprodáno.) — Milujeme M instead; name another to change it"."""
    pref = product_in_tag(report, tp.tag, tp.fallback_from)
    issue = tp.preferred_issue or "unavailable"
    why = issue if tp.fallback_too_big else f"unavailable ({issue})"
    if tp.category == "line" and tp.rows:
        pick = product_in_tag(report, tp.tag, tp.rows[0].sku)
        return f"{tp.tag.name}: {pref} {why} — {pick} instead; name another to change it"
    return f"{tp.tag.name}: {pref} {why}; name another to change it"


def _solo_reminders(report) -> list[str]:
    """A `solo` tag that buys without its preferred product, or is unavailable for want of it: a reminder (§2) from
    the start of the streak's first run (§6.13), shown in the first 3 normally finished non-dry runs that bought the
    tag without it or found the tag unavailable; a run that didn't buy the tag shows it no more than it uses it up."""
    world = report.world
    out = []
    for tp in report.plan.plans:
        if tp.fallback_from is None or tp.category not in ("line", "unavailable"):
            continue
        since = fallback_since(world, tp.tag, report.now)
        if _reminder_visible(report, since=since, any_day=True,
                             counts=lambda run, tag=tp.tag: showed_fallback(world, tag, run)):
            out.append(f"- {solo_fallback_text(report, tp)}")
    return out


def _notices(report) -> list[str]:
    out = []
    for tp in report.plan.plans:
        td, tag = tp.td, tp.tag
        if td is None:
            continue
        if tag.long_life and tag.stockup_mode and not td.window and td.history:
            d = max(ev.date for ev in td.history) + timedelta(days=WINDOW_LONG)
            if _notice_visible(report, d):
                out.append(f"- {tag.name} dropped out: not bought for 12 months — buy it once and it's back")
        if tag.policy == "variety":
            last: dict[int, date] = {}
            for ev in td.history:
                for s, u in ev.units.items():
                    if (u or 0) > EPS:
                        last[s] = ev.date
            for s, d0 in sorted(last.items()):
                if (tag.tag_id, s) in report.replay.dropped:
                    continue
                d = d0 + timedelta(days=td.window_days)
                if _notice_visible(report, d):
                    months = round(td.window_days / 30.44)
                    out.append(f"- {_pname(report, s)} left the {tag.name} mix: not bought for {months} months")
    return out


def _storage_label(name: str) -> str:
    return name.replace("-", " ").capitalize()


def weighed_counting(world, tag) -> str | None:
    """§8.1/§8.2: how a tag with weighed products counts them — in pieces (`pcs` tag) or by weight (`kg` tag)."""
    if not any(world.skus[s].weighted for s in tag.skus if s in world.skus):
        return None
    return "counted in pieces" if tag.base_unit == "pcs" else "by weight"


def _mode_of(report, tag) -> str | None:
    if not tag.long_life:
        return None
    a = report.plan.assessed.get(tag.tag_id) if report.plan else None
    return tag.stockup_mode or (a.mode if a else None)


def policy_text(report, tag) -> str:
    """A tag's policy for the summaries; for `solo`, its preferred product (§8.1, §8.2)."""
    if tag.policy == "solo" and tag.preferred_sku is not None:
        return f"solo: prefers {product_in_tag(report, tag, tag.preferred_sku)}"
    return tag.policy


def product_in_tag(report, tag, sku: int) -> str:
    """A product's name as in Rohlik, with its pack text where another product of its tag has the same name (§8.1)."""
    return report.world.display_name(sku, tag)


def _pname(report, sku: int) -> str:
    """A product's name in a summary section (§8.1), resolved through its current tag."""
    return report.world.display_name(sku)


def _check_lines(report) -> list[str]:
    """§8.1 'Check these tags' (not on the first run, whose setup report asks instead): the products the agent
    tagged since the previous run, grouped by storage tag, each tag with its policy (for `solo`, its preferred
    product), stock-up mode and counting; possible wrong merges. The user's own corrections are never listed back.
    While `preferred_check` is set (the 1 -> 2 migration set preferred products), every `solo` tag is listed too,
    except those whose preferred product the user named himself, until a non-dry run finishes normally."""
    world = report.world
    if report.first_run:
        return []
    groups: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    if report.prev_run is not None:
        since = report.prev_run.started_at
        for s in world.skus.values():
            if s.tag_id and s.assigned_by == "agent" and s.updated_at > since:
                groups[s.storage][s.tag_id].append(s)
    if world.preferred_check:
        listed = {tag_id for by_tag in groups.values() for tag_id in by_tag}
        for tag in world.tags.values():
            if (tag.policy == "solo" and tag.preferred_sku in world.skus and tag.preferred_source != "user"
                    and tag.tag_id not in listed):
                groups[world.skus[tag.preferred_sku].storage][tag.tag_id] = []
    if not groups:
        return []
    out, names = [], set()
    for storage in sorted(groups):
        for tag_id in sorted(groups[storage], key=lambda t: world.tags[t].sort_key):
            tag = world.tags[tag_id]
            names.add(tag.name)
            attrs = [policy_text(report, tag), _mode_of(report, tag), weighed_counting(world, tag)]
            products = ", ".join(world.display_name(x.sku, tag)
                                 for x in sorted(groups[storage][tag_id], key=lambda x: (x.name, x.sku)))
            out.append(f"- {_storage_label(storage)} — {tag.name} ({', '.join(a for a in attrs if a)})"
                       + (f": {products}" if products else ""))
    out.extend(f"- possible wrong merge: {line}" for line in wrong_merges(world) if line.split(":")[0] in names)
    out.append("")
    out.append("Tell me if any is wrong; otherwise they stay.")
    return out


def wrong_merges(world) -> list[str]:
    """SKUs of one tag bought together in at least 2 orders (variety/assorted excepted)."""
    out = []
    pairs: dict[tuple[str, int, int], int] = defaultdict(int)
    for o in world.orders.values():
        by_tag = defaultdict(list)
        for s in o.lines:
            tag = world.tag_of(s)
            if tag is not None and tag.policy not in ("variety", "assorted"):
                by_tag[tag.tag_id].append(s)
        for tag_id, skus in by_tag.items():
            skus.sort()
            for i in range(len(skus)):
                for j in range(i + 1, len(skus)):
                    pairs[(tag_id, skus[i], skus[j])] += 1
    for (tag_id, a, b), n in sorted(pairs.items(), key=lambda kv: (world.tags[kv[0][0]].sort_key, kv[0][1:])):
        if n >= 2:
            out.append(f"{world.tags[tag_id].name}: {world.display_name(a)} and {world.display_name(b)} were bought "
                       f"together in {n} orders")
    return out


# ------------------------------------------------------------------------------------------ totals
def unit_price(report, sku: int) -> float | None:
    lv = report.live.get(sku)
    return lv.unit_price if lv else None


def totals(report) -> dict:
    cart = report.cart
    out = {"cart_total_czk": None, "projected_total_czk": None, "projected_incomplete": False,
           "agent_subtotal_czk": 0.0, "topup_spend_czk": 0.0, "topup_saved_czk": 0.0}
    agent = 0.0
    for tp in report.plan.plans:
        if tp.category != "line":
            continue
        for r in tp.rows:
            price = unit_price(report, r.sku) or 0.0
            agent += (r.normal_units + r.stockup_units) * price
        out["topup_spend_czk"] += tp.extra
        out["topup_saved_czk"] += tp.saved
    out["agent_subtotal_czk"] = agent
    if report.dry:
        observed = cart.total or 0.0
        proj = observed
        for sku, target in sorted(report.targets.items()):
            price = unit_price(report, sku)
            if price is None:
                out["projected_incomplete"] = True
                continue
            proj += (target - cart.qty(sku)) * price
        out["cart_total_czk"] = observed
        out["projected_total_czk"] = proj
    else:
        final = report.final_cart or cart
        out["cart_total_czk"] = final.total
    for k in list(out):
        if isinstance(out[k], float):
            out[k] = round(out[k], 2)
    return out


def _min_order_warning(report, tot: dict) -> str | None:
    cart = report.final_cart or report.cart
    minimum = cart.minimal_order
    total = tot["projected_total_czk"] if report.dry else tot["cart_total_czk"]
    if minimum is None or total is None or total >= minimum - EPS:
        return None
    left = "" if report.xtra_remaining is None else f"; Xtra orders without the minimum left: {report.xtra_remaining}"
    which = "projected cart total" if report.dry else "cart total"
    return f"the {which} {total:.0f} CZK is {minimum - total:.0f} CZK below the {minimum:.0f} CZK minimum order{left}"


# ------------------------------------------------------------------------------------------ render
def _header(report) -> list[str]:
    h = report.t + timedelta(days=int(report.cadence + 1e-9)) if report.t else None
    lines = [f"**Delivery {fmt_day(report.t)}** ({report.t_source}) · next expected ~{fmt_day(h)}"]
    banners = []
    if report.dry:
        banners.append("**Dry run — nothing was written to the cart.**")
    if report.in_progress:
        banners.append("**Checkout in progress — re-run shortly.**")
    if report.different_account:
        banners.append("**Different account than last time.**")
    for oid, d in report.vanished_kept:
        banners.append(f"Order {oid} is no longer listed (still counted as on its way for {fmt_day(d)}).")
    for oid in report.vanished_dropped:
        banners.append(f"Order {oid} disappeared without being delivered or cancelled.")
    for oid, d in report.overdue_deliveries:
        banners.append(f"Delivery of {fmt_day(d)} (order {oid}) is overdue.")
    return lines + banners


def _section(title: str, lines: list[str]) -> list[str]:
    return ["", f"### {title}", *lines] if lines else []


SETUP_TAG_CHECK = ("Look over the tags, policies, preferred products and stock-up modes above, and tell me what's "
                   "wrong: split, merge or rename a tag, move a product, change a policy, a preferred product or a "
                   "stock-up mode. Anything you don't correct stays.")


def render(report) -> str:
    """§8.1: short by design. In a writing run the cart shows what was added; `data` keeps every plan line. A first
    run's `summary_md` is the compact setup report (§8.2); the full one goes to its file."""
    if report.first_run:
        return render_setup(report, compact=True)
    return "\n".join(_plan_sections(report)).strip() + "\n"


def _plan_sections(report) -> list[str]:
    out = list(_header(report))
    tot = totals(report)
    if report.dry:
        out += _section("Would add", _would_add_lines(report))
    out += _section("Topped up on sale", _topup_lines(report))
    out += _section("Suggestions (not added)", _suggestion_lines(report))
    out += _section("Reminders", _reminder_lines(report))
    out += _section("Check these tags", _check_lines(report))
    warns = []
    mo = _min_order_warning(report, tot)
    if mo:
        warns.append(f"- {mo}")
    for name in report.skipped_tags:
        warns.append(f"- {name}: changed in the cart while I was writing — left as you have it")
    for w in report.writes:
        if w.outcome == "failed":
            warns.append(f"- could not set {_pname(report, w.sku)} to {_num(w.target)} (was {_num(w.before)})")
    if report.t_source != "your date" and report.t:
        warns.append(f"- planned for delivery on {fmt_day(report.t)} — re-run with `--date` if you pick another day")
    if report.recovered_run:
        warns.append(f"- the previous run ({report.recovered_run}) was interrupted; I finished its bookkeeping from "
                     f"your cart")
    for w in report.warnings:
        if not w.startswith("order ") and not w.startswith("could not set"):
            warns.append(f"- {w}")
    out += _section("Warnings", warns)
    tl = []
    if report.dry:
        tl.append(f"- cart now: {tot['cart_total_czk']:.0f} CZK · projected: {tot['projected_total_czk']:.0f} CZK"
                  + (" (incomplete: some prices unknown)" if tot["projected_incomplete"] else ""))
    elif tot["cart_total_czk"] is not None:
        tl.append(f"- cart total: {tot['cart_total_czk']:.0f} CZK")
    if tot["topup_spend_czk"] > 0:
        tl.append(f"- top-ups: {tot['topup_spend_czk']:.0f} CZK, saving {tot['topup_saved_czk']:.0f} CZK")
    out += _section("Totals", tl)
    if report.checkout_link:
        out += _section("Checkout", [f"- {report.checkout_link}"])
    return out


def render_pending(report) -> str:
    """§6.6: a checkout Rohlik hasn't published yet; the summary is only the banner."""
    return "**Looks like you just checked out — re-run once the order shows up.**\n"


def pending_data(report) -> dict:
    return {"run_id": report.run_id, "T": None, "dry_run": True, "first_run": False, "checkout": "pending",
            "plan": [], "removals": [], "writes": []}


SETUP_INTRO = [
    "**What tags are.** A tag is a *need*, not a product: products in one tag are interchangeable for your "
    "household. Usage and purchases are counted per tag, and I pick the product within a tag by its policy:",
    "- `cheapest` always takes the cheapest available one;",
    "- `solo` takes your preferred product, whatever else you buy now and then, and the cheapest available one only "
    "when it is unavailable — guessed where one product makes up over 40 % of a tag's purchases and costs more than "
    "the others you bought;",
    "- `variety` mixes the types you eat from one shared slot, such as sliced cheeses, in your recent proportions;",
    "- `assorted` takes one each of the several cheapest, for things you buy a few kinds of at once, such as pasta "
    "shapes.",
    "\"Same use\" matters: a 1 kg curd bucket for baking and 250 g tubs for eating are different tags. A wrong "
    "grouping causes wrong substitutions and quantities; a tag that is too narrow may never reach 3 purchases, and "
    "is then never added.", "",
    "**Stock-up.** Short-life tags are bought one delivery at a time. Long-life tags (every product keeps) have a "
    "stock-up mode, guessed from the prices you paid over the last 12 months:",
    "- `always`: when it runs low, your usual batch (your average purchase) is added, whatever the price;",
    "- `on_sale`: on a sale of at least 35 %, it is topped up to your largest purchase, once per sale; when it runs "
    "low without one, you only get a reminder.",
    "Each guessed mode is shown with its evidence below; tags that qualify later default to `always`. Every order "
    "counts, whoever filled the cart: when a quantity is wrong, change it in the cart, and I learn from that order."]

HOW_TO_CORRECT = ("**How to correct**, just tell me: split, merge or rename a tag; move a product; change a policy, a "
                  "`solo` tag's preferred product, a stock-up mode or a product's storage tag; count a tag's weighed "
                  "products (⚖) in pieces or by weight; group types eaten from one shared slot into a `variety` tag, "
                  "or kinds bought several at a time where only the price matters into an `assorted` tag; edit a "
                  "storage tag's stock-up flag; ignore a product; mark a tag \"never\".")


def render_setup(report, compact: bool) -> str:
    """§8.2: the full setup report (written to its file), or the compact version that `summary_md` carries. Both
    are grouped by storage tag and end with the tag check."""
    out: list[str] = []
    if compact:
        out += _header(report)
        out += ["", "## Setup", "", "This first run is a dry run: it learned your tags and shows the cart it would "
                "build. The full setup report is in the file below; here are the tags that matter now."]
    else:
        out += ["## Setup", "", *SETUP_INTRO]
    out += ["", "**Your tags, by storage**"]
    out += _mapping(report, compact)
    ignored = sorted((s for s in report.world.skus.values() if s.ignored), key=lambda x: (x.name, x.sku))
    if ignored:
        out += ["", "**Ignored products:** " + ", ".join(f"{s.name} (by {s.assigned_by})" for s in ignored)]
    if not compact:
        out += ["", HOW_TO_CORRECT]
    out += ["", _history_line(report), "", _backtest_line(report)]
    out += ["", "## The cart this run would have built (nothing was written)", ""]
    out += _plan_sections(report)[0 if not compact else len(_header(report)):]
    if compact and report.report_path:
        out += ["", f"Full report: {report.report_path}"]
    out += ["", "## Check these tags", ""]
    guesses = _guess_list(report)
    if guesses:
        out += ["I guessed these from your purchases:", *guesses, ""]
    out.append(SETUP_TAG_CHECK)
    return "\n".join(out).strip() + "\n"


def _times_bought(world) -> dict[int, int]:
    times: dict[int, int] = defaultdict(int)
    for o in world.orders.values():
        for s in o.lines:
            times[s] += 1
    return times


def _texts_of(world, sku: int) -> str | None:
    info = world.skus[sku]
    return differing_texts(info.text, [o.lines[sku].text for o in world.orders.values() if sku in o.lines],
                           info.weighted)


def _mapping(report, compact: bool) -> list[str]:
    """§8.2 'Full mapping, grouped by storage tag': within each storage tag (a tag sits under the storage tag of its
    most-bought product), the tags by how often they are bought. The compact version lists in full the tags that
    can be added or suggested and every flagged tag, one line each, and counts the others per storage tag."""
    world = report.world
    times = _times_bought(world)
    merges = {line.split(":")[0] for line in wrong_merges(world)}
    datas = {tp.tag.tag_id: tp.td for tp in report.plan.plans} if report.plan else {}
    groups: dict[str, list] = defaultdict(list)
    for tag in world.tags.values():
        if tag.skus:
            top = min(tag.skus, key=lambda s: (-times[s], world.skus[s].name, s))
            groups[world.skus[top].storage].append(tag)
    out: list[str] = []
    for storage in sorted(groups):
        flag = "yes" if world.eligible.get(storage) else "no"
        out += ["", f"**{_storage_label(storage)}** (stock-up {flag})"]
        rest = 0
        for tag in sorted(groups[storage], key=lambda t: (-sum(times[s] for s in t.skus), t.sort_key)):
            flags = _flags(report, tag, merges)
            td = datas.get(tag.tag_id)
            active = td is not None and (td.n >= GATE_SUGGEST or td.managed)
            if compact and not (active or flags):
                rest += 1
                continue
            flag_txt = f" — **{', '.join(flags)}**" if flags else ""
            counting = weighed_counting(world, tag)
            head = (f"- **{tag.name}** · {_setup_policy(report, tag)} · {tag.base_unit}"
                    f"{' · ' + counting if counting else ''}{_setup_mode(report, tag)}{flag_txt}")
            products = []
            for s in sorted(tag.skus, key=lambda x: (-times[x], world.skus[x].name, x)):
                info = world.skus[s]
                size = size_of(info.text, tag.base_unit, info.upp, info.upp_unit, info.weighted)
                size_txt = f"{_num(size.value)} {tag.base_unit}" if size.known else "size unknown"
                if info.weighted:
                    size_txt = "⚖ " + size_txt
                other = f" · stored as {info.storage}" if info.storage != storage else ""
                texts = _texts_of(world, s)
                texts_txt = f" · order-line texts: {texts}" if texts else ""
                products.append((info.name, size_txt, times[s], other + texts_txt))
            if compact:
                out.append(head + ": " + "; ".join(f"{n} · {z} · {t}×{x}" for n, z, t, x in products))
            else:
                out.append(head)
                out += [f"  - {n} · {z} · bought {t}×{x}" for n, z, t, x in products]
        if rest:
            out.append(f"- {rest} more tag{'s' if rest != 1 else ''}, each bought once or not recently")
    return out


def _flags(report, tag, merges: set[str]) -> list[str]:
    """§8.2 highlights: multi-product, solo/variety/assorted, a guess, a possible wrong merge, an unknown pack size,
    differing order-line texts."""
    world = report.world
    flags = []
    if len(tag.skus) > 1:
        flags.append(f"{len(tag.skus)} products")
    if tag.policy in ("solo", "variety", "assorted"):
        flags.append(tag.policy)
    if report.plan and tag.tag_id in report.plan.guesses:
        flags.append("guessed solo")
    a = report.plan.assessed.get(tag.tag_id) if report.plan else None
    if tag.long_life and not tag.stockup_mode and a is not None and a.from_prices:
        flags.append("guessed mode")
    if tag.name in merges:
        flags.append("possible wrong merge")
    if any(not size_of(world.skus[s].text, tag.base_unit, world.skus[s].upp, world.skus[s].upp_unit,
                       world.skus[s].weighted).known for s in tag.skus):
        flags.append("unknown pack size")
    if any(_texts_of(world, s) for s in tag.skus):
        flags.append("order-line texts differ")
    return flags


def _setup_policy(report, tag) -> str:
    """The policy in the mapping; for `solo`, its preferred product, and for a guess its evidence (§6.13)."""
    guess = report.plan.guesses.get(tag.tag_id) if report.plan else None
    if guess is None:
        return policy_text(report, tag)
    return f"solo: prefers {product_in_tag(report, tag, guess.sku)} — {_guess_evidence(guess)}"


def _guess_evidence(guess) -> str:
    return (f"{guess.purchases} of {guess.total} purchases, {guess.pricier_pct} % pricier than the others you "
            f"bought")


def _guess_list(report) -> list[str]:
    """§8.2: every guess in one short list, with its evidence in a few words: the solo guesses with their preferred
    products, and the `on_sale` modes."""
    world = report.world
    out = []
    for tag_id, guess in sorted(report.plan.guesses.items(), key=lambda kv: world.tags[kv[0]].sort_key):
        tag = world.tags[tag_id]
        out.append(f"- {tag.name}: solo, prefers {product_in_tag(report, tag, guess.sku)} ({guess.purchases} of "
                   f"{guess.total} purchases, {guess.pricier_pct} % pricier)")
    for tag_id, a in sorted(report.plan.assessed.items(), key=lambda kv: world.tags[kv[0]].sort_key):
        if a.mode == "on_sale" and a.from_prices:
            out.append(f"- {world.tags[tag_id].name}: on_sale ({_num(a.on_sale_units)} of {_num(a.total_units)} "
                       f"packs at least {round(report.min_discount * 100)} % below today's price)")
    return out


def _history_line(report) -> str:
    world = report.world
    hist = sorted(o.delivery_date for o in world.orders.values() if o.delivery_date < report.e)
    add = sug = rest = 0
    for tp in report.plan.plans:
        td = tp.td
        if td is None:
            continue
        if td.n >= GATE_ADD or td.managed:
            add += 1
        elif td.n == GATE_SUGGEST:
            sug += 1
        else:
            rest += 1
    span = (hist[-1] - hist[0]).days if len(hist) > 1 else 0
    return (f"**History:** {len(hist)} delivered orders over {span} days; you order about every "
            f"{_num(round(report.cadence, 1))} days. {add} tags will be added when they run low, {sug} only "
            f"suggested (bought twice), {rest} not proposed yet (bought once).")


def _backtest_line(report) -> str:
    from .backtest import interpretation, pct, run_backtest

    bt = run_backtest(report.world)
    if bt.evaluated:
        return (f"**Backtest on your history** ({bt.evaluated} orders): {bt.proposed_per_order:.1f} tags per order; "
                f"precision {pct(bt.precision_same)} same order, {pct(bt.precision_next)} this or next; recall "
                f"{bt.recall_lines:.0%} of lines, {bt.recall_spend:.0%} of spend — {interpretation(bt)}")
    return "**Backtest:** not enough history yet (it needs half a year)."


def _setup_mode(report, tag) -> str:
    """A long-life tag's mode in the setup mapping: guessed from prices with its evidence (§6.14, §8.2)."""
    if not tag.long_life:
        return ""
    if tag.stockup_mode:
        return f" · stock-up: {tag.stockup_mode}"
    a = report.plan.assessed.get(tag.tag_id)
    if a is None:
        return " · stock-up: `always` once bought 3 times"
    if not a.from_prices:
        return f" · stock-up: {a.mode} (no price evidence)"
    return (f" · stock-up: {a.mode} — {_num(a.on_sale_units)} of {_num(a.total_units)} packs bought at least "
            f"{round(report.min_discount * 100)} % below today's regular price")


def render_needs_tags(listing: list[dict]) -> str:
    """§7.1 step 6, short (§9): a count and the first 10 products; `data.skus` holds the full list for tagging."""
    lines = [f"**{len(listing)} product{'s need' if len(listing) != 1 else ' needs'} tags** before I can plan. Tag "
             f"every product in `data.skus` per `reference/tagging.md`, run `tags apply`, then run again.", ""]
    for item in listing[:SUMMARY_NAMES_MAX]:
        size = f"{_num(item['size'])} {item['unit']}" if item.get("size") else "no size parsed"
        texts = f" · order-line texts: {item['line_texts']}" if item.get("line_texts") else ""
        lines.append(f"- {item['sku']} · {item['name']} · '{item.get('text') or ''}'{' ⚖' if item['weighted'] else ''}"
                     f" · {size} · bought {item['times_bought']}×{texts}")
    if len(listing) > SUMMARY_NAMES_MAX:
        lines.append(f"- … and {len(listing) - SUMMARY_NAMES_MAX} more in `data.skus`")
    return "\n".join(lines) + "\n"


def render_insufficient(count: int) -> str:
    return (f"**Not enough history yet.** I found {count} delivered order{'s' if count != 1 else ''} in the last "
            f"183 days; I need 3 to learn your pace. Order a few times, then run me again.\n")


# ------------------------------------------------------------------------------------------ data JSON
def data(report) -> dict:
    """§8.3: the summary is short, so `data` keeps every plan line with its reasons ("why X?")."""
    world = report.world
    why = {}
    for tp in report.plan.plans:
        if tp.category == "line" and tp.rows:
            why[tp.tag.tag_id] = " · ".join(b for b in (_why(tp, report), _which(tp, report)) if b)
    plan_rows = []
    for r in report.plan.rows:
        tag = world.tags[r.tag_id]
        plan_rows.append({"tag": tag.name, "tag_id": tag.tag_id, "sku": r.sku, "name": _name(report, r.sku),
                          "reason": r.reason, "normal_units": r.normal_units, "stockup_units": r.stockup_units,
                          "sale_until": r.sale_until.isoformat() if r.sale_until else None,
                          "size": tidy(r.size), "size_unit": r.size_unit,
                          "target": tidy(report.targets.get(r.sku)), "why": why.get(r.tag_id),
                          "fallback_from": r.fallback_from})
    removals = [{"sku": s, "name": _name(report, s), "target": tidy(t)} for s, t in sorted(report.targets.items())
                if s not in {r.sku for r in report.plan.rows}]
    out = {
        "run_id": report.run_id,
        "T": report.t.isoformat() if report.t else None,
        "T_source": report.t_source,
        "C": tidy(report.cadence),
        "next_delivery": (report.t + timedelta(days=int(report.cadence + 1e-9))).isoformat() if report.t else None,
        "dry_run": report.dry,
        "first_run": report.first_run,
        "checkout": "in_progress" if report.in_progress else None,
        "plan": plan_rows,
        "removals": removals,
        "locked": sorted(tp.tag.name for tp in report.plan.plans if tp.category == "locked"),
        "kept_unchecked": sorted(tp.tag.name for tp in report.plan.plans if tp.category == "kept"),
        "paused": sorted(world.tags[t].name for t in report.replay.paused if t in world.tags),
        "assessed": {world.tags[k].name: v.mode for k, v in sorted(report.plan.assessed.items())},
        "guessed_preferred": {world.tags[k].name: {"sku": g.sku, "name": _name(report, g.sku),
                                                   "purchases": g.purchases, "of": g.total,
                                                   "pricier_pct": g.pricier_pct}
                              for k, g in sorted(report.plan.guesses.items())},
        "solo_fallbacks": [solo_fallback_text(report, tp) for tp in report.plan.plans
                           if tp.fallback_from is not None and tp.category in ("line", "unavailable")],
        "writes": [{"sku": w.sku, "tool": w.tool, "from": tidy(w.before), "to": tidy(w.target), "outcome": w.outcome}
                   for w in report.writes],
        "skipped_tags": report.skipped_tags,
        "totals": totals(report),
    }
    if report.first_run:
        out["report_path"] = report.report_path  # §8.2, §8.3: the full setup report
    return out
