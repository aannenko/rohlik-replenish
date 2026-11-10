"""Backtest (§13.4): metric definitions and the backtest-only pause heuristic on small synthetic CSV datasets, the
`backtest` command, and the acceptance on the dataset provided with the spec (§15) when it is available."""

import csv
import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

from engine import cli
from engine.backtest import load_csv_world, run_backtest

PRODUCTS = {  # product_id: (name, tag, storage_class, textual_amount, price)
    1: ("Mléko polotučné 1,5 %", "Milk", "chilled-dairy", "1 l", 25.0),
    2: ("Chléb kváskový", "Bread", "bakery", "500 g", 40.0),
}
START = date(2025, 1, 6)

# §13.4 baseline (proxy_backtest_v11.py, raw sale quantities, the provided 44-order dataset)
BASELINE = {"precision_same_order": 0.48, "precision_this_or_next_order": 0.65, "recall_lines": 0.28,
            "recall_spend": 0.28}


def write_dataset(directory: Path, orders: list[set[int]] | dict[int, set[int]]) -> Path:
    """Orders from START: a list gives weekly orders (orders[k] = the product IDs of order k, one unit each); a dict
    maps a week number to its order's product IDs."""
    directory.mkdir(parents=True, exist_ok=True)
    purchases, totals = [], []
    weeks = orders.items() if isinstance(orders, dict) else enumerate(orders)
    for k, skus in weeks:
        day = (START + timedelta(days=7 * k)).isoformat()
        for sku in sorted(skus):
            name, _, _, text, price = PRODUCTS[sku]
            purchases.append([1000 + k, day, sku, name, 1, "ks", price, price, "", "", text, "False"])
        totals.append([1000 + k, day, sum(PRODUCTS[s][4] for s in skus), len(skus)])
    with open(directory / "purchases.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["order_id", "delivered_date", "product_id", "product_name", "quantity", "unit", "unit_price_czk",
                    "line_price_czk", "sale_indicator", "original_price_czk", "textual_amount", "weighted"])
        w.writerows(purchases)
    with open(directory / "products.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["product_id", "product_name", "tag", "storage_class", "base_unit", "base_amount_per_pack",
                    "orders_count", "total_spend_czk"])
        for sku, (name, tag, storage, _, _) in PRODUCTS.items():
            w.writerow([sku, name, tag, storage, "", "", "", ""])
    with open(directory / "orders.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["order_id", "delivered_date", "total_price_czk", "line_count"])
        w.writerows(totals)
    return directory


def backtest(directory: Path, raw_units: bool = False):
    world, notes = load_csv_world(directory, raw_units=raw_units)
    return run_backtest(world, raw_units=raw_units), notes


def test_regular_tag_scores_perfectly_and_only_orders_after_183_days_count(tmp_path):
    res, _ = backtest(write_dataset(tmp_path, [{1}] * 40))
    first_evaluated = next(k for k in range(40) if 7 * k >= 183)
    assert res.evaluated == 40 - first_evaluated
    assert [r["date"] for r in res.per_order][0] == (START + timedelta(days=7 * first_evaluated)).isoformat()
    assert res.proposed_per_order == 1.0
    assert (res.precision_same, res.precision_next, res.recall_lines, res.recall_spend) == (1.0, 1.0, 1.0, 1.0)


def test_missed_tag_pauses_after_two_misses_and_resumes_when_bought(tmp_path):
    # Milk is bought weekly, skipped in orders 30-35, bought again from order 36; bread every week.
    orders = [{1, 2} if k < 30 or k >= 36 else {2} for k in range(40)]
    res, _ = backtest(write_dataset(tmp_path, orders))
    rows = {int(r["order_id"]) - 1000: r for r in res.per_order}
    assert min(rows) == 27
    assert [rows[k]["proposed"] for k in range(27, 40)] == [2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 2, 2, 2]
    assert rows[30]["prec"] == rows[31]["prec"] == 0.5  # milk proposed but not bought: two misses
    assert rows[29]["prec_next"] == 1.0 and rows[30]["prec_next"] == 0.5  # order 29's milk is in order 29 itself
    assert rows[36]["rec_lines"] == 0.5  # milk paused, but bought
    assert rows[36]["rec_spend"] == pytest.approx(40.0 / 65.0)  # bread's share of the order's spend
    assert rows[37]["prec"] == 1.0


def test_too_short_history_says_more_history_is_needed(tmp_path, capsys, monkeypatch):
    directory = write_dataset(tmp_path / "csv", [{1, 2}] * 10)
    res, _ = backtest(directory)
    assert res.evaluated == 0 and any("more history is needed" in n for n in res.notes)
    monkeypatch.setenv("ROHLIK_REPLENISH_DATA_DIR", str(tmp_path / "data"))
    code = cli.main(["backtest", "--csv", str(directory)])
    env = json.loads(capsys.readouterr().out)
    assert code == 0 and env["status"] == "ok" and "more history is needed" in env["summary_md"]


def test_no_proposals_reads_n_a_in_the_backtest_command_and_the_setup_report(tmp_path, capsys, monkeypatch):
    """§13.4, §8.2: every evaluated order has an empty proposed set, so precision is undefined: "n/a"."""
    directory = write_dataset(tmp_path / "csv", {0: {1}, 27: {2}, 28: {2}, 29: {1}})
    res, _ = backtest(directory)
    assert res.evaluated == 3 and res.proposed_per_order == 0 and res.precision_same is None
    monkeypatch.setenv("ROHLIK_REPLENISH_DATA_DIR", str(tmp_path / "data"))
    code = cli.main(["backtest", "--csv", str(directory)])
    env = json.loads(capsys.readouterr().out)
    assert code == 0 and env["status"] == "ok" and "n/a" in env["summary_md"], env
    assert env["data"]["precision_same_order"] is None
    # the same in a first run's setup report (the review's case: four orders of four one-purchase tags)
    from tests.helpers import Scenario, at

    s = Scenario(tmp_path / "s")
    mapping = {}
    for i, day in enumerate(("2026-03-01", "2026-09-01", "2026-09-08", "2026-09-15")):
        s.product(901 + i, f"Product {i} 1 kg", "1 kg", 10.0)
        s.delivered(day, {901 + i: 1})
        mapping[901 + i] = (f"product {i}", "cheapest", "kg", "dry")
    env = s.first_runs(at("2026-09-28", 9), mapping, date_="2026-09-30")
    assert "precision n/a same order, n/a this or next" in env.summary_md, env.summary_md


def test_backtest_command_envelopes(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ROHLIK_REPLENISH_DATA_DIR", str(tmp_path / "data"))
    directory = write_dataset(tmp_path / "csv", [{1, 2}] * 40)
    for extra in ([], ["--raw-units"]):
        code = cli.main(["backtest", "--csv", str(directory), *extra])
        env = json.loads(capsys.readouterr().out)
        assert code == 0 and env["status"] == "ok", env
        assert env["data"]["evaluated_orders"] == 13 and env["data"]["precision_same_order"] == 1.0
        assert ("raw sale units" in env["summary_md"]) == bool(extra)
    code = cli.main(["backtest", "--csv", str(tmp_path / "missing")])
    env = json.loads(capsys.readouterr().out)
    assert code == 1 and env["status"] == "error" and env["data"]["stage"] == "backtest"
    code = cli.main(["backtest"])  # no --csv and no account history yet
    env = json.loads(capsys.readouterr().out)
    assert code == 1 and env["data"]["stage"] == "backtest"


# ------------------------------------------------------------------------------ acceptance on the §15 dataset
def _dataset() -> Path | None:
    """The §15 dataset is real purchase history and lives outside the repository: REPLENISH_BACKTEST_CSV points at it."""
    explicit = os.environ.get("REPLENISH_BACKTEST_CSV")
    if not explicit:
        return None
    assert (Path(explicit) / "purchases.csv").is_file(), f"REPLENISH_BACKTEST_CSV={explicit} has no purchases.csv"
    return Path(explicit)


DATASET = _dataset()


@pytest.mark.skipif(DATASET is None, reason="the §15 dataset is not available (set REPLENISH_BACKTEST_CSV)")
@pytest.mark.parametrize("raw_units", [False, True], ids=["native-units", "raw-units"])
def test_acceptance_on_the_provided_dataset(raw_units):
    with open(DATASET / "orders.csv", encoding="utf-8-sig", newline="") as fh:
        days = sorted(date.fromisoformat(r["delivered_date"]) for r in csv.DictReader(fh))
    res, notes = backtest(DATASET, raw_units)
    metrics = res.as_dict()
    print(f"\n{'raw' if raw_units else 'native'} units: {json.dumps(metrics, ensure_ascii=False)}")
    assert len(days) == 44
    assert res.evaluated == sum(1 for x in days if x >= days[0] + timedelta(days=183))
    for name, baseline in BASELINE.items():
        assert metrics[name] >= baseline - 0.03, f"{name} {metrics[name]:.3f} is below the baseline {baseline} - 3 points"
    assert any("longer than 64 characters" in n for n in notes)  # one tag name in the dataset has 65
