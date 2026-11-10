"""§13.1 input boundary, cadence, rate and stock transitions, due/need."""

import random
from datetime import date, datetime, timedelta, timezone

import pytest

from engine.numerics import median, percentile_nearest_rank, round_half_up, round_up
from engine.planner import cadence_of
from engine.sizes import delivered_line_base, parse, size_of
from engine.stock import RateHistory, StockEvent, StockSim, project
from engine.timeutil import PRAGUE, end_of_day, local_date, parse_rohlik_time, parse_slot

UTC = timezone.utc


# ------------------------------------------------------------------------------ sizes and grammar
@pytest.mark.parametrize("text,expected", [
    ("2 x 100 g", (0.2, "kg")),
    ("6×1,5l", (9.0, "l")),
    ("cca 190 g", (0.19, "kg")),
    ("308,8 m", (308.8, "m")),
    ("1 kg", (1.0, "kg")),
    ("500 ml", (0.5, "l")),
    ("30 ks", (30.0, "pcs")),
    ("0,75 kg", (0.75, "kg")),
    ("Cca 1,5 kg", (1.5, "kg")),
])
def test_grammar_parses(text, expected):
    amount, unit = parse(text)
    assert unit == expected[1]
    assert amount == pytest.approx(expected[0])


@pytest.mark.parametrize("text", ["80 mytí", "1 kytice", "", None, "500 g balení", "g 500", "0 g"])
def test_grammar_rejects(text):
    assert parse(text) is None


def test_size_live_text_vs_line_text_vs_override():
    assert size_of("500 g", "kg").value == 0.5
    assert size_of("500 g", "kg", upp_override=0.45, upp_unit="kg").value == 0.45
    other = size_of("500 ml", "kg")
    assert other.value is None and other.note == "other_unit"
    # a stored size whose unit differs from the tag's current base unit counts as unknown (never reinterpreted)
    stale = size_of("500 g", "l", upp_override=0.5, upp_unit="kg")
    assert stale.value is None and stale.note == "override_other_unit"
    assert size_of("80 mytí", "pcs").note == "unparsed"


def test_line_base_quantities():
    # weighed line: actual weight, claims scale it, nothing claimed -> weight alone
    assert delivered_line_base(4, 0, 0.92, True, 0.2) == pytest.approx(0.92)
    assert delivered_line_base(4, 1, 0.92, True, 0.2) == pytest.approx(0.69)
    # otherwise (quantity - claimed) x size of the line's own text
    assert delivered_line_base(3, 1, None, False, 0.25) == pytest.approx(0.5)
    # unknowns
    assert delivered_line_base(None, 0, None, False, 0.25) is None
    assert delivered_line_base(2, 0, None, False, None) is None
    # never below 0
    assert delivered_line_base(1, 3, None, False, 1.0) == 0.0


def test_weighed_piece_text_varies_per_delivery():
    """A weighed line uses that delivery's own per-piece text when there is no actual weight."""
    lemon_small = delivered_line_base(2, 0, None, True, size_of("180 g", "kg").value)
    lemon_big = delivered_line_base(2, 0, None, True, size_of("300 g", "kg").value)
    assert lemon_small == pytest.approx(0.36) and lemon_big == pytest.approx(0.6)


def test_a_weighed_sku_in_a_pcs_tag_is_one_per_piece_or_its_units_per_pack():
    """§6.2: one piece, whatever its weight and text; a weighed bunch gives its piece count as an override."""
    for text in ("cca 190 g", "cca 0,65 kg", "1 ks", None):
        assert size_of(text, "pcs", weighted=True).value == 1.0
    assert size_of("cca 0,65 kg", "pcs", upp_override=4, upp_unit="pcs", weighted=True).value == 4.0
    assert size_of("cca 190 g", "kg", weighted=True).value == pytest.approx(0.19), "a kg tag counts its weight"
    other = size_of("cca 190 g", "pcs")
    assert other.value is None and other.note == "other_unit", "only a weighed SKU counts 1 per piece"
    stale = size_of("cca 0,65 kg", "pcs", upp_override=0.65, upp_unit="kg", weighted=True)
    assert stale.value is None and stale.note == "override_other_unit", "a stored size is never reinterpreted"
    assert size_of("cca 0,65 kg", "kg", upp_override=4, upp_unit="pcs", weighted=True).value is None


def test_a_weighed_line_in_a_pcs_tag_counts_pieces_minus_claimed_pieces_and_ignores_its_weight():
    # 6 pieces weighing 1.5 kg, 1 piece claimed: 5 pieces in a pcs tag; by weight 1.25 kg in a kg tag
    assert delivered_line_base(6, 1, 1.5, False, size_of("cca 190 g", "pcs", weighted=True).value) == 5.0
    assert delivered_line_base(6, 1, 1.5, True, size_of("cca 190 g", "kg", weighted=True).value) == pytest.approx(1.25)
    # a weighed bunch of 4: two bunches, one claimed, whatever they weighed
    bunch = size_of("cca 0,65 kg", "pcs", upp_override=4, upp_unit="pcs", weighted=True).value
    assert delivered_line_base(2, 1, 1.43, False, bunch) == 4.0


# ------------------------------------------------------------------------------ numerics
def test_rounding_and_statistics():
    assert round_up(2.0000000001) == 2 and round_up(2.1) == 3
    assert round_half_up(2.5) == 3 and round_half_up(2.49) == 2
    assert median([1, 3, 2, 4]) == 2.5 and median([5, 1, 3]) == 3
    assert percentile_nearest_rank(list(range(1, 11)), 0.9) == 9
    assert percentile_nearest_rank([1, 2, 3], 0.9) == 3


# ------------------------------------------------------------------------------ time
def test_prague_dates_follow_dst():
    summer = parse_rohlik_time("2026-07-06T15:09:36.000+0200")
    winter = parse_rohlik_time("2026-01-06T23:30:00.000+0100")
    assert summer.utcoffset() == timedelta(0)
    assert local_date(summer) == date(2026, 7, 6)
    assert local_date(winter) == date(2026, 1, 6)
    # 23:30 UTC on 25 Oct 2026 (after the switch back) is already 26 Oct locally
    assert local_date(datetime(2026, 10, 25, 23, 30, tzinfo=UTC)) == date(2026, 10, 26)
    assert local_date(datetime(2026, 7, 25, 22, 30, tzinfo=UTC)) == date(2026, 7, 26)
    d, end = parse_slot("2026-10-26 08:00-09:00")
    assert d == date(2026, 10, 26) and end == datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
    assert end_of_day(date(2026, 3, 29)) == datetime(2026, 3, 29, 22, 0, tzinfo=UTC)


# ------------------------------------------------------------------------------ cadence
def _dates(start: str, gaps: list[int]) -> list[date]:
    out = [date.fromisoformat(start)]
    for g in gaps:
        out.append(out[-1] + timedelta(days=g))
    return out


def test_cadence_median_clamp_override_and_default():
    assert cadence_of(_dates("2026-01-01", [7, 7, 8, 6]), None) == 7
    assert cadence_of(_dates("2026-01-01", [7, 8]), None) == 7.5  # fractional C
    assert cadence_of(_dates("2026-01-01", [2, 2, 2]), None) == 4  # clamped
    assert cadence_of(_dates("2026-01-01", [30, 40, 35]), None) == 21
    assert cadence_of(_dates("2026-01-01", [7]), None) == 7  # fewer than 2 gaps
    assert cadence_of(_dates("2026-01-01", [3, 3, 3]), 9.5) == 9.5  # the user's override
    # only the last 8 gaps count, and repeated dates are one date
    many = _dates("2026-01-01", [30] * 5 + [5] * 8)
    assert cadence_of(many + many[-1:], None) == 5


# ------------------------------------------------------------------------------ rate
def _t(day: float) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day)


def ev(day, q, user=True, placed=None, stock=0.0, oid=None, arrival=None):
    placed = _t(day - 1) if placed is None else placed
    return StockEvent(float(day), placed, oid or int(day * 10), q, stock, user,
                      arrival or _t(day + 0.4))


def test_rate_interval_excludes_the_event_at_b():
    hist = [ev(0, 2), ev(10, 2, user=False), ev(20, 6)]  # every event counts, whatever its origin
    rates = RateHistory(hist, 183)
    assert rates.interval(21) == (0, 20)
    assert rates.rate(21) == pytest.approx(4 / 20)


def test_rate_fallback_to_the_two_most_recent_history_dates():
    # one window date -> the two most recent distinct history dates
    rates = RateHistory([ev(0, 1), ev(10, 1), ev(30, 1)], 10)
    assert rates.interval(31) == (10.0, 30.0)
    assert RateHistory([ev(30, 1)], 10).interval(31) is None  # no interval, no rate
    assert rates.interval(30) == (0.0, 10.0), "the rate at d uses only the events dated before d"


# ------------------------------------------------------------------------------ stock transitions
def test_each_past_day_consumes_the_rate_known_that_day():
    """§13.1: a history whose rate steps from 1 to 2 units a week replays each stretch at its own rate, and a
    replay at today's rate gives a different stock. Agent-only events (no resets): 1 unit on days 0 and 7, 2 on
    days 14 and 21, e = T = 28. r_d is none up to day 7, 1/7 from day 8 (and before it: the first rate), 4/21
    from day 22: 6 bought - 22/7 - 6 * 4/21 eaten = 12/7 on hand. At today's rate 4/21 throughout, the shelf
    runs empty twice and 4/3 is left."""
    hist = [ev(0, 1, user=False), ev(7, 1, user=False), ev(14, 2, user=False), ev(21, 2, user=False)]
    rates = RateHistory(hist, 183)
    assert rates.rate(7) is None
    assert [rates.rate(d) for d in (8, 15, 21, 22, 28)] == pytest.approx([1 / 7, 1 / 7, 1 / 7, 4 / 21, 4 / 21])
    today = rates.rate(28)
    per_day = project(hist, [], today, 28.0, 28.0, 35.0, past=rates)
    at_today = project(hist, [], today, 28.0, 28.0, 35.0)
    assert per_day.on_hand_t == pytest.approx(12 / 7)
    assert at_today.on_hand_t == pytest.approx(4 / 3)


def test_days_before_the_first_rate_use_the_first_rate():
    """2 units on day 0, 1 on days 7 and 14 (agent-only). r_d first exists on day 8 (2/7), so days 0-7 eat 2/7 a
    day too: 5/7 is left on day 15. Eating nothing before day 8 would leave 2, today's rate (3/14) throughout
    11/14. When the first rate is today's, every past day eats it."""
    hist = [ev(0, 2, user=False), ev(7, 1, user=False), ev(14, 1, user=False)]
    rates = RateHistory(hist, 183)
    assert rates.rate(8) == pytest.approx(2 / 7) and rates.rate(15) == pytest.approx(3 / 14)
    assert project(hist, [], rates.rate(15), 15.0, 15.0, 22.0, past=rates).on_hand_t == pytest.approx(5 / 7)
    assert project(hist, [], rates.rate(15), 15.0, 15.0, 22.0).on_hand_t == pytest.approx(11 / 14)
    two = hist[:2]  # the first rate is today's: 2 eaten by day 7, then 1 more arrives and 2/7 of it is eaten
    rates = RateHistory(two, 183)
    assert project(two, [], rates.rate(8), 8.0, 8.0, 15.0, past=rates).on_hand_t == pytest.approx(5 / 7)


def _oracle_on_hand(hist: list[StockEvent], window: int, e: int) -> float:
    """§6.9 literally, one day at a time: every day d applies its events, then eats r_d from §6.8 evaluated at d,
    FIFO with same-day normal batches before stock-up batches."""
    def rate_at(d):
        pos = [x for x in hist if x.day < d and x.q > 1e-9]
        win = sorted({x.day for x in pos if x.day > d - window})
        dates = sorted({x.day for x in pos})
        if len(win) >= 2:
            a, b = win[0], win[-1]
        elif len(dates) >= 2:
            a, b = dates[-2], dates[-1]
        else:
            return None
        return sum(x.q for x in pos if a <= x.day < b) / (b - a)

    start = int(min(x.day for x in hist))
    known = [rate_at(d) for d in range(start, e + 1)]
    first = next(r for r in known if r is not None)
    batches = []  # [day, stockup, seq, arrival_time, qty]
    seq = 0
    for d in range(start, e):
        for x in sorted((x for x in hist if x.day == d), key=lambda x: x.key):
            if x.user and x.q > 1e-9:
                batches = [b for b in batches if b[1] or not b[3] < x.placement]
            stock = min(x.stockup_base, x.q)
            for flag, qty in ((True, stock), (False, x.q - stock)):
                if qty > 1e-9:
                    seq += 1
                    batches.append([x.day, flag, seq, x.arrival_time, qty])
        want = known[d - start] if known[d - start] is not None else first
        for b in sorted(batches, key=lambda b: (b[0], b[1], b[2])):
            take = min(b[4], want)
            b[4] -= take
            want -= take
        batches = [b for b in batches if b[4] > 1e-9]
    return sum(b[4] for b in batches)


@pytest.mark.parametrize("seed", range(30))
def test_per_day_rates_equal_a_day_by_day_replay(seed):
    """The rate steps are evaluated only where r_d can change (an event entering the history, or leaving the
    window); a literal day-by-day replay of §6.8/§6.9 must give the same stock."""
    rnd = random.Random(1000 + seed)
    window = rnd.choice([183, 365])
    hist, day = [], 0
    while day < 600:
        q = 0.0 if rnd.random() < 0.1 else round(rnd.uniform(0.2, 6), 3)
        stock = round(q * rnd.random(), 3) if rnd.random() < 0.2 else 0.0
        hist.append(StockEvent(float(day), _t(day - rnd.uniform(0.5, 2)), day, q, stock, rnd.random() < 0.5,
                               _t(day + 0.4)))
        day += rnd.choice([1, 3, 5, 7, 9, 14, 30, 60, 200]) if rnd.random() < 0.3 else rnd.randint(2, 9)
    e = day
    rates = RateHistory(hist, window)
    today = rates.rate(e)
    p = project(hist, [], today, float(e), float(e), e + 7.0, past=rates)
    assert p.on_hand_t == pytest.approx(_oracle_on_hand(hist, window, e), abs=1e-6)


# ------------------------------------------------------------------------------ single-rate mechanics
def test_fifo_and_same_day_normal_before_stockup():
    sim = StockSim(1.0)
    sim.apply(ev(0, 2, stock=1.0))  # 1 stock-up + 1 normal on day 0
    sim.advance(1.0)
    left = sorted((b.stockup, round(b.qty, 6)) for b in sim.batches)
    assert left == [(True, 1.0)], "normal goes first on the same date"


def test_reset_drops_only_batches_that_arrived_before_the_decision():
    """§13.1: O1 = 4 units placed 6 Oct arriving 8 Oct; O2 = 1 unit, user-initiated, placed 7 Oct, arriving 9 Oct.
    At T = 9 Oct, 4 units are on hand, not 1."""
    o1 = StockEvent(8.0, _t(6), 1, 4.0, 0.0, True, _t(8.4))
    o2 = StockEvent(9.0, _t(7), 2, 1.0, 0.0, True, _t(9.4))
    p = project([], [o1, o2], 0.0001, 5.0, 9.0, 10.0)
    assert p.on_hand_t == pytest.approx(5.0, abs=1e-3)


def test_same_day_reset_uses_slot_end_and_unknown_slot_counts_as_end_of_day():
    morning = StockEvent(5.0, _t(4), 1, 3.0, 0.0, True, _t(5.3))  # arrived 07:12 UTC
    noon_decision = StockEvent(5.0, _t(5.5), 2, 1.0, 0.0, True, _t(6.3))
    sim = StockSim(0.0)
    for e in sorted([morning, noon_decision], key=lambda x: x.key):
        sim.apply(e)
    assert sim.on_hand() == pytest.approx(1.0), "the morning batch had arrived before the noon decision"
    unknown_slot = StockEvent(5.0, _t(4), 1, 3.0, 0.0, True, end_of_day(date(2026, 1, 6)))
    sim = StockSim(0.0)
    for e in sorted([unknown_slot, noon_decision], key=lambda x: x.key):
        sim.apply(e)
    assert sim.on_hand() == pytest.approx(4.0), "an unknown slot counts as the end of the day"


def test_agent_only_events_and_q_zero_never_reset():
    hist = [ev(0, 5), ev(2, 1, user=False), ev(3, 0)]
    sim = StockSim(0.0)
    for e in hist:
        sim.apply(e)
    assert sim.on_hand() == pytest.approx(6.0)


def test_stockup_survives_a_reset_and_projection_starts_at_the_first_event():
    """A stock-up bought long before the window is still on the shelf."""
    old = ev(0, 10, stock=8.0)
    later = ev(200, 1)
    p = project([old, later], [], 0.01, 300.0, 300.0, 307.0)
    # 10 - 0.01*200 = 8 left (normal eaten first), then the user reset at day 200 drops normal but keeps stock-up
    assert p.on_hand_t == pytest.approx(8.0 - 0.01 * 100 + 1.0 - 0.0, abs=0.02)


def test_no_write_off():
    p = project([ev(0, 100)], [], 1.0, 50.0, 50.0, 57.0)
    assert p.on_hand_t == pytest.approx(50.0)


def _search_min_u(history, incoming, rate, e, t, h, extra, past=None):
    lo, hi = 0.0, rate * (h - t) + 1.0
    if project(history, incoming, rate, e, t, h, extra, past=past).shortfall <= 1e-9:
        return 0.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if project(history, incoming, rate, e, t, h, extra, new_batch=mid, past=past).shortfall <= 1e-9:
            hi = mid
        else:
            lo = mid
    return hi


@pytest.mark.parametrize("seed", range(40))
def test_need_equals_search_over_u(seed):
    """§13.1: the closed form equals a search over u, including empty stock, a user order already placed for day 3
    and the planned batch surviving that order's reset. Odd seeds replay the past at per-day rates (§6.9)."""
    rnd = random.Random(seed)
    rate = rnd.uniform(0.1, 2.0)
    e = 100.0
    t = e + rnd.choice([0, 1, 2])
    h = t + rnd.uniform(3, 14)
    now = _t(e) + timedelta(hours=8)
    history = []
    day = 20.0
    while day < e:
        history.append(StockEvent(day, _t(day - 1), int(day), rnd.uniform(0, 6), 0.0, rnd.random() < 0.6,
                                  _t(day + 0.4)))
        day += rnd.uniform(3, 12)
    incoming = []
    if rnd.random() < 0.7:  # a user order already placed (before now) for day 3 after e
        incoming.append(StockEvent(e + 3, now - timedelta(hours=rnd.uniform(1, 30)), 9001, rnd.uniform(0, 5), 0.0,
                                   True, _t(e + 3.4)))
    if rnd.random() < 0.4:
        incoming.append(StockEvent(e + rnd.choice([0, 1, 5, 9]), now - timedelta(hours=2), 9002, rnd.uniform(0, 4),
                                   0.0, rnd.random() < 0.5, _t(e + 6)))
    extra = []
    if rnd.random() < 0.4:  # the user's own cart line: a reset at T, placed now
        extra.append(StockEvent(t, now, 2**62, rnd.uniform(0.1, 3), 0.0, True, end_of_day(date.fromordinal(1))))
    if seed % 10 == 0:
        history = []  # empty stock
    past = RateHistory(history, 183) if seed % 2 else None
    p = project(history, incoming, rate, e, t, h, extra, past=past)
    u = _search_min_u(history, incoming, rate, e, t, h, extra, past)
    assert p.need == pytest.approx(u, abs=1e-6)
