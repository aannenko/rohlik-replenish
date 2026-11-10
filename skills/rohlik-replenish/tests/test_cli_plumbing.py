"""CLI envelopes, exit codes, `--fixtures` end to end on the synthetic household (and on the private recorded
fixtures, where present), determinism (§13.5), and the plumbing items of §13.1."""

import json
from pathlib import Path

import pytest

from engine import cli
from engine.fake import FakeRohlik
from engine.mcp_transport import MemoryKeyring, SecretStore, TokenStore
from engine.paths import AuthFile
from engine.rohlik import (AuthRequired, BudgetExhausted, NotAllowed, RawResult, RohlikClient, ToolFailure,
                           TransportError, extract_payload)
from tests import synthetic
from tests.helpers import RECORDED as FIX
from tests.helpers import recorded

NOW = "2026-10-26T12:00:00+01:00"


def invoke(capsys, monkeypatch, data_dir, *argv):
    monkeypatch.setenv("ROHLIK_REPLENISH_DATA_DIR", str(data_dir))
    code = cli.main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out)


def first_runs(capsys, monkeypatch, d, fix=FIX, min_skus=1):
    code, env = invoke(capsys, monkeypatch, d, "run", "--fixtures", str(fix), "--now", NOW, "--date", "2026-10-28")
    assert code == 2 and env["status"] == "needs_tags"
    assert len(env["data"]["skus"]) >= min_skus
    code, env = invoke(capsys, monkeypatch, d, "tags", "apply", str(fix / "tags.json"))
    assert code == 0, env
    code, env = invoke(capsys, monkeypatch, d, "run", "--fixtures", str(fix), "--now", NOW, "--date", "2026-10-28")
    assert code == 0 and env["status"] == "dry_run", env["summary_md"][:500]
    return env


def test_synthetic_fixtures_end_to_end(synthetic_fixtures, tmp_path, capsys, monkeypatch):
    """The CLI from needs_tags to a writing run, as on the recorded household below, on a household built to show
    each part of the setup report (tests/synthetic.py), so it runs on every machine."""
    import re

    from engine.summary import SETUP_TAG_CHECK

    fix = synthetic_fixtures
    env = first_runs(capsys, monkeypatch, tmp_path, fix, min_skus=len(synthetic.TAGS))
    md = env["summary_md"]
    # §8.2: summary_md is the compact version; the full report is in its file, linked from the summary
    path = Path(env["data"]["report_path"])
    assert path.name == "setup-report.md" and path.parent.parent == tmp_path and f"Full report: {path}" in md
    full = path.read_text(encoding="utf-8")
    assert len(md.splitlines()) < len(full.splitlines()), "the compact version is shorter"
    for text in (md, full):
        assert "## Setup" in text and "Backtest on your history" in text and "nothing was written" in text
        # §8.2, §13.2: grouped by storage tag, and both end with the tag check
        assert text.index("## Setup") < text.index("**Your tags, by storage**") < text.index(
            "## The cart this run would have built")
        assert text.rstrip().endswith(SETUP_TAG_CHECK)
        assert "## Check these tags\n\nI guessed these from your purchases:\n" in text
        groups = re.findall(r"\n\*\*([A-Z][a-z -]+)\*\* \(stock-up (?:yes|no)\)\n", text)
        assert groups == ["Bakery", "Chilled", "Drinks", "Drugstore household", "Dry", "Fresh produce", "Frozen",
                          "Other"]
    assert md.startswith("**Delivery 28 Oct** (your date)"), "the compact version starts with the header"
    assert "What tags are" in full and "What tags are" not in md and "How to correct" in full
    assert "- **mango** · cheapest · pcs" in full and "**mango**" not in md, "the compact version folds once-bought tags"
    # the first run's guesses, with their evidence (§6.13, §6.14)
    assert ("- **yogurt plain** · solo: prefers Alpine plain yogurt — 5 of 10 purchases, 22 % pricier than the "
            "others you bought · kg — **3 products, solo, guessed solo**") in full
    assert "- yogurt plain: solo, prefers Alpine plain yogurt (5 of 10 purchases, 22 % pricier)" in md
    assert "- **milk 1.5%** · cheapest · l — **2 products**\n" in full, "the favourite milk is the cheaper: no guess"
    assert "- dishwasher cleaner: on_sale (3 of 3 packs at least 35 % below today's price)" in md
    # weighed products are counted in pieces (a bunch gives its piece count) or by weight
    produce = full.split("**Fresh produce** (stock-up no)\n")[1].split("\n\n")[0]
    assert "- **bananas** · cheapest · pcs · counted in pieces" in produce
    assert "  - Banana 1 pc · ⚖ 1 pcs · bought 34×" in produce and "  - Bananas, bunch · ⚖ 4 pcs · bought 1×" in produce
    assert "- **potatoes** · cheapest · kg · by weight" in produce
    assert "order-line texts: '2 l' in 3 orders, '500 ml' now" in full
    planned = {r["sku"]: r["normal_units"] + r["stockup_units"] for r in env["data"]["plan"]
               if r["reason"] in ("due", "stockup")}
    assert planned, "the synthetic household has due tags"
    code, env2 = invoke(capsys, monkeypatch, tmp_path, "run", "--fixtures", str(fix), "--now",
                        "2026-10-26T12:30:00+01:00", "--date", "2026-10-28")
    assert code == 0 and env2["status"] == "ok"
    assert "report_path" not in env2["data"], "only the first run writes the setup report"
    writes = env2["data"]["writes"]
    assert writes and all(w["outcome"] == "confirmed" for w in writes)
    assert {w["sku"]: w["to"] for w in writes} == planned, "the writing run builds the cart the dry run showed"
    code, st = invoke(capsys, monkeypatch, tmp_path, "status", "--fixtures", str(fix), "--now", NOW)
    assert code == 0 and "lines I own in the cart: " in st["summary_md"]
    code, tl = invoke(capsys, monkeypatch, tmp_path, "tags", "list", "--tag", "eggs")
    assert code == 0
    assert "eggs · solo, prefers Free-range eggs L (104) (set by agent)" in tl["summary_md"]
    assert "(104) · **preferred**" in tl["summary_md"]


@recorded
def test_recorded_fixtures_end_to_end(tmp_path, capsys, monkeypatch):
    import re

    from engine.summary import SETUP_TAG_CHECK

    env = first_runs(capsys, monkeypatch, tmp_path, min_skus=301)
    md = env["summary_md"]
    # §8.2: summary_md is the compact version; the full report is in its file, linked from the summary
    path = Path(env["data"]["report_path"])
    assert path.name == "setup-report.md" and path.parent.parent == tmp_path and f"Full report: {path}" in md
    full = path.read_text(encoding="utf-8")
    assert len(md.splitlines()) < len(full.splitlines()) / 2, "the compact version is much shorter"
    for text in (md, full):
        assert "## Setup" in text and "Backtest on your history" in text and "nothing was written" in text
        # §8.2, §13.2: grouped by storage tag, and both end with the tag check
        assert text.index("## Setup") < text.index("**Your tags, by storage**") < text.index(
            "## The cart this run would have built")
        assert text.rstrip().endswith(SETUP_TAG_CHECK)
        assert "## Check these tags\n\nI guessed these from your purchases:\n" in text
        groups = re.findall(r"\n\*\*([A-Z][a-z -]+)\*\* \(stock-up (?:yes|no)\)\n", text)
        assert len(groups) == len(set(groups)) >= 8 and groups == sorted(groups), groups
    assert md.startswith("**Delivery 28 Oct** (your date)"), "the compact version starts with the header"
    assert "What tags are" in full and "What tags are" not in md and "How to correct" in full
    dry = full.split("**Dry** (stock-up yes)\n")[1].split("\n\n")[0]
    assert "- **milk 1.5% uht** · cheapest · l" in dry
    # loose produce eaten a piece at a time is counted in pieces; a weighed bunch gives its piece count
    produce = full.split("**Fresh produce** (stock-up no)\n")[1].split("\n\n")[0]
    assert "- **bananas** · cheapest · pcs · counted in pieces" in produce
    assert "  - Banány, balení · ⚖ 4 pcs · bought" in produce and "  - Banán 1 ks · ⚖ 1 pcs · bought" in produce
    assert "- **potatoes** · cheapest · kg · by weight" in produce
    # a guessed solo tag shows its evidence in the mapping and in the guess list (§6.13, §8.2); its two
    # products share a name, so the pack text tells them apart
    assert ("- **smoked salmon sliced** · solo: prefers Pappudia Norský uzený losos plátky (200 g) — 7 of 11 "
            "purchases, 18 % pricier than the others you bought") in full
    assert "- smoked salmon sliced: solo, prefers Pappudia Norský uzený losos plátky (200 g) (7 of 11 purchases, " \
           "18 % pricier)" in md
    assert "- tomato paste: solo, prefers Giana Rajčatový protlak (2 of 3 purchases, 39 % pricier)" in md, \
        "no pack text when the names differ"
    # an order-line text that differs from today's is shown (the spec's own example)
    assert "order-line texts: '2 l' in 3 orders, '500 ml' now" in full
    assert env["data"]["plan"], "the recorded household has due tags"
    code, env2 = invoke(capsys, monkeypatch, tmp_path, "run", "--fixtures", str(FIX), "--now",
                        "2026-10-26T12:30:00+01:00", "--date", "2026-10-28")
    assert code == 0 and env2["status"] == "ok"
    assert "report_path" not in env2["data"], "only the first run writes the setup report"
    assert env2["data"]["writes"] and all(w["outcome"] == "confirmed" for w in env2["data"]["writes"])
    code, st = invoke(capsys, monkeypatch, tmp_path, "status", "--fixtures", str(FIX), "--now", NOW)
    assert code == 0 and "lines I own in the cart" in st["summary_md"]
    code, tl = invoke(capsys, monkeypatch, tmp_path, "tags", "list", "--tag", "milk 1.5% uht")
    assert code == 0 and "milk 1.5% uht" in tl["summary_md"]
    code, tl = invoke(capsys, monkeypatch, tmp_path, "tags", "list", "--tag", "eggs")
    assert "eggs · solo, prefers Pohodová vejce Podestýlková L (1316375) (set by agent)" in tl["summary_md"]
    assert "(1316375) · **preferred**" in tl["summary_md"]


@pytest.mark.parametrize("household", ["synthetic", pytest.param("recorded", marks=recorded)])
def test_determinism_byte_identical_data(household, synthetic_fixtures, tmp_path, capsys, monkeypatch):
    fix, min_skus = (synthetic_fixtures, len(synthetic.TAGS)) if household == "synthetic" else (FIX, 301)
    a = first_runs(capsys, monkeypatch, tmp_path / "a", fix, min_skus)
    b = first_runs(capsys, monkeypatch, tmp_path / "b", fix, min_skus)
    # the report's path names the data directory; everything else is byte-identical (§13.5)
    path_a, path_b = Path(a["data"].pop("report_path")), Path(b["data"].pop("report_path"))
    assert path_a.read_bytes() == path_b.read_bytes()
    assert json.dumps(a["data"], sort_keys=False) == json.dumps(b["data"], sort_keys=False)
    assert a["summary_md"].replace(str(path_a), "<path>") == b["summary_md"].replace(str(path_b), "<path>")


def test_exit_codes_and_error_envelopes(synthetic_fixtures, tmp_path, capsys, monkeypatch):
    code, env = invoke(capsys, monkeypatch, tmp_path, "status")
    assert code == 1 and env["status"] == "error" and env["data"]["wrote"] is False and env["data"]["actions"]
    code, env = invoke(capsys, monkeypatch, tmp_path, "run", "--now", NOW)
    assert code == 1 and env["data"]["stage"] == "arguments", "--now needs --fixtures"
    code, env = invoke(capsys, monkeypatch, tmp_path, "run", "--bogus")
    assert code == 1 and env["status"] == "error"
    code, env = invoke(capsys, monkeypatch, tmp_path, "run", "--fixtures", str(synthetic_fixtures), "--now", NOW,
                       "--date", "2020-01-01")
    assert code == 1 and env["data"]["stage"] == "options"


def test_lock_refuses_a_concurrent_command(tmp_path, capsys, monkeypatch):
    from engine.oslock import DataLock

    lock = DataLock(tmp_path)
    lock.acquire()
    try:
        code, env = invoke(capsys, monkeypatch, tmp_path, "status")
        assert code == 1 and env["data"]["stage"] == "lock"
        assert "another run is in progress" in env["data"]["actions"][0]
    finally:
        lock.release()


def test_failed_upcoming_read_reports_stage_and_no_write(synthetic_fixtures, tmp_path, capsys, monkeypatch):
    fix = tmp_path / "fix"
    fix.mkdir()
    for f in synthetic_fixtures.iterdir():
        (fix / f.name).write_bytes(f.read_bytes())
    (fix / "settings.json").write_text(json.dumps({"failures": {"fetch_orders:upcoming": "upstream error"}}))
    code, env = invoke(capsys, monkeypatch, tmp_path / "d", "run", "--fixtures", str(fix), "--now", NOW)
    assert code == 1 and env["data"]["stage"] == "sync" and env["data"]["wrote"] is False
    assert env["data"]["actions"]


def test_no_session_is_auth_required_without_network(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("engine.mcp_transport.SecretStore", lambda *a, **k: SecretStore(backend=MemoryKeyring()))
    code, env = invoke(capsys, monkeypatch, tmp_path, "run")
    assert code == 2 and env["status"] == "auth_required"


# ------------------------------------------------------------------------------ plumbing (§13.1)
def test_allowlist_refuses_before_sending():
    fake = FakeRohlik()
    client = RohlikClient(fake, sleep=lambda s: None)
    for tool in ("clear_cart", "cancel_order", "reserve_timeslot", "submit_claim", "remove_cart_item"):
        with pytest.raises(NotAllowed):
            client.call(tool)
    with pytest.raises(NotAllowed):
        client.call("fetch_orders", order_type="both", limit=15)
    assert fake.calls == [], "nothing was sent"


def test_every_call_carries_the_constant_context():
    fake = FakeRohlik()
    RohlikClient(fake, sleep=lambda s: None).get_cart()
    assert fake.calls[0][1]["context"] == "Automated grocery replenishment for the account owner."


def test_result_unwrapping_and_is_error():
    payload = {"success": True, "orders": []}
    text = json.dumps(payload) + "\n\n" + json.dumps({"result": payload})
    assert extract_payload(RawResult(texts=[text])) == payload
    assert extract_payload(RawResult(structured={"result": {"a": 1}})) == {"a": 1}

    class Err:
        def call(self, tool, args, timeout):
            return RawResult(texts=["boom"], is_error=True)
    with pytest.raises(ToolFailure):
        RohlikClient(Err(), sleep=lambda s: None).get_cart()

    class Bad:
        def call(self, tool, args, timeout):
            return RawResult(structured={"success": False, "message": "nope"})
    with pytest.raises(ToolFailure, match="nope"):
        RohlikClient(Bad(), sleep=lambda s: None).get_cart()


def test_empty_upcoming_answer_is_valid():
    fake = FakeRohlik()
    assert RohlikClient(fake, sleep=lambda s: None).fetch_orders("upcoming") == []


def test_reads_retry_with_backoff_and_writes_never_retry():
    class Flaky:
        def __init__(self):
            self.n = 0

        def call(self, tool, args, timeout):
            self.n += 1
            if self.n < 3:
                raise TransportError("reset")
            return RawResult(structured={"success": True, "data": {"items": {}}})
    sleeps = []
    flaky = Flaky()
    RohlikClient(flaky, sleep=sleeps.append).get_cart()
    assert flaky.n == 3 and sleeps == [1.0, 2.0]
    flaky2 = Flaky()
    with pytest.raises(TransportError):
        RohlikClient(flaky2, sleep=sleeps.append).update_item(1, 2)
    assert flaky2.n == 1


def test_details_batch_tool_error_is_retried_per_id_and_missing_ids_are_absent():
    fake = FakeRohlik(products={1: {"name": "a"}, 2: {"name": "b"}})
    calls = {"n": 0}

    def fail_first(tool, args):
        if tool == "get_product_details":
            calls["n"] += 1
            if calls["n"] == 1:
                raise ToolFailure("get_product_details", "one product in the batch broke it")
    fake.before_call = fail_first
    out = RohlikClient(fake, sleep=lambda s: None).product_details([1, 2, 3])
    assert set(out) == {1, 2}
    assert [len(a["product_ids"]) for t, a in fake.calls if t == "get_product_details"] == [3, 1, 1, 1]


def test_details_batch_failing_on_transport_is_not_split_into_single_ids():
    """An outage split into single IDs only multiplies it; the read fails after its retries."""
    fake = FakeRohlik(products={i: {"name": str(i)} for i in range(1, 41)})

    def outage(tool, args):
        if tool == "get_product_details":
            raise TransportError("timed out")  # retryable, like a 5xx
    fake.before_call = outage
    with pytest.raises(TransportError):
        RohlikClient(fake, sleep=lambda s: None).product_details(range(1, 41))
    sizes = [len(a["product_ids"]) for t, a in fake.calls if t == "get_product_details"]
    assert sizes == [20, 20, 20, 20], "one batch, retried 3 times, then the read fails"


def test_details_read_with_no_requested_id_answered_fails_when_required():
    client = RohlikClient(FakeRohlik(products={}), sleep=lambda s: None)
    assert client.product_details([7, 8]) == {}
    with pytest.raises(ToolFailure, match="none of the 2 requested products"):
        client.product_details([7, 8], require_any=True)
    assert client.product_details([], require_any=True) == {}


def test_read_budget_stops_a_hung_run():
    """§4.4: all of a run's reads share 10 minutes; a read that would start after it fails at once."""
    clock = {"t": 0.0}

    class Hung:
        calls = 0

        def call(self, tool, args, timeout):
            Hung.calls += 1
            clock["t"] += timeout  # every attempt times out after 30 s
            raise TransportError("timed out")

    client = RohlikClient(Hung(), sleep=lambda s: clock.__setitem__("t", clock["t"] + s), clock=lambda: clock["t"])
    errors = []
    for _ in range(10):
        try:
            client.get_cart()
        except BudgetExhausted as exc:
            errors.append(exc)
            break
        except TransportError:
            continue
    assert errors, "the budget ends the reads"
    assert clock["t"] >= 600 and Hung.calls <= 24, f"{Hung.calls} attempts in {clock['t']:.0f} s"
    with pytest.raises(BudgetExhausted):
        client.get_user_info()
    with pytest.raises(TransportError):  # writes aren't reads: no budget, no retry
        client.update_item(1, 2)


def test_tokens_survive_from_login_to_the_next_command(tmp_path):
    import asyncio

    from mcp.shared.auth import OAuthToken

    kr = MemoryKeyring()
    clock = lambda: 1_000_000.0  # noqa: E731
    login_store = TokenStore(SecretStore(backend=kr), AuthFile(tmp_path), clock=clock)
    long_token = "x" * 3000  # longer than one Windows credential: split across numbered entries
    asyncio.run(login_store.set_tokens(OAuthToken(access_token=long_token, expires_in=300, refresh_token=None)))
    assert any(name.endswith(".1") for _, name in kr.data), "split across numbered entries"
    run_store = TokenStore(SecretStore(backend=kr), AuthFile(tmp_path), clock=lambda: 1_000_100.0)
    tok = asyncio.run(run_store.get_tokens())
    assert tok.access_token == long_token and tok.expires_in == 200
    assert run_store.has_session()
    assert AuthFile(tmp_path).load()["refresh_token_issued"] is False
    assert "x" * 50 not in (tmp_path / "auth.json").read_text(), "secrets never go to files"
    from datetime import datetime, timezone
    assert not run_store.write_window_ok(datetime.fromtimestamp(1_000_250.0, tz=timezone.utc)), "under 60 s left"
    assert run_store.write_window_ok(datetime.fromtimestamp(1_000_200.0, tz=timezone.utc))


def test_auth_required_from_transport_is_reported(tmp_path):
    class NoAuth:
        def call(self, tool, args, timeout):
            raise AuthRequired("no session")
    with pytest.raises(AuthRequired):
        RohlikClient(NoAuth(), sleep=lambda s: None).get_user_info()
