# rohlik-replenish

A skill that fills your [Rohlik.cz](https://www.rohlik.cz) cart for the next delivery from your own order history —
without asking what to buy. You review the cart and check out in the Rohlik app or website; the skill never places,
changes or cancels an order. The agent instructions are in [`SKILL.md`](SKILL.md); the
[specification](../../docs/spec.md) is v1.18.

## Install uv

The engine runs with [uv](https://docs.astral.sh/uv/) **0.5.20 or newer** (the oldest version that reads the
committed lockfile). Install it user-locally, no administrator rights needed:

- Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
- macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`

uv provisions a supported Python (3.11–3.14) and the locked packages on first use; there is no build step. On a newer
system Python (e.g. 3.15) uv downloads a managed 3.14 instead of building packages from source.

## Log in

```
uv run replenish.py login
```

This registers the engine as an OAuth client with Rohlik (once), opens your browser at Rohlik's login page and
prints the URL on stderr. Log in and approve; the page then says "Rohlik login complete". It waits up to 5 minutes.
Rohlik issues a refresh token, and each run refreshes the 8-hour access token itself, so you log in again only when a
run reports `auth_required` (routine; a run never opens a browser).

Secrets (client secret, access and refresh tokens) live only in the OS keyring (Windows Credential Manager, macOS
Keychain, Secret Service), under the service name `rohlik-replenish`; long tokens are split across numbered entries.
Legacy e-mail/password logins are never used.

## Commands

All commands print one JSON envelope on stdout: `{"status", "summary_md", "data", "warnings"}`. Exit codes: 0 for
`ok`/`dry_run`, 2 for `needs_tags`/`auth_required`/`insufficient_history`, 1 for `error`.

| Command | What it does |
| --- | --- |
| `login` | Interactive login. |
| `run [--date YYYY-MM-DD] [--dry-run]` | One run: sync orders, plan, fill the cart. The first run is always a dry setup run: its summary is a compact setup report, and the full one is written to `<account_id>/setup-report.md` (`data.report_path`). Without `--date`, the delivery date is the earliest free slot in the next 4 days; when none is free, or some days' slots can't be read, it is tomorrow, and a warning says which. |
| `tags list [--tag TAG]` | Product tags, their products, storage tags, stock-up modes, `solo` tags' preferred products, and order-line texts that differ from today's. |
| `tags apply FILE [--source agent\|user]` | Assign products to tags from a JSON file (see `reference/tagging.md`). |
| `tags rename TAG NEW_NAME`, `tags merge FROM INTO`, `tags set TAG [--policy …] [--preferred SKU] [--stockup-mode …] [--note …] [--base-unit …]`, `tags storage-set NAME --stockup yes\|no` | Tag corrections (each takes `--source agent\|user`). `--preferred` names the product a `solo` tag buys and makes the tag `solo`; on an existing tag only `--source user` may. |
| `prefs never add\|remove TAG`, `prefs cadence DAYS\|auto` | Standing preferences. |
| `backtest [--csv DIR] [--raw-units]` | How well the model would have predicted past orders (your history, or a CSV dataset). |
| `status` | History, cadence, last run, owned cart lines, locks, paused tags, recent precision and recall. |

Test-only options: `--fixtures DIR` (recorded responses replace Rohlik) and `--now ISO` (with `--fixtures`, or in
`backtest`).

## Data directory

`platformdirs.user_data_dir("rohlik-replenish", appauthor=False)` — on Windows
`%LOCALAPPDATA%\rohlik-replenish`, on macOS `~/Library/Application Support/rohlik-replenish`, on Linux
`~/.local/share/rohlik-replenish`. It holds `auth.json` (the OAuth registration and the authorization server's
public metadata, whether a refresh token was issued, the last account ID — never secrets), `lock`, an optional
`config.toml`, and per Rohlik account a SQLite database (`<account_id>/replenish.db`, schema version 2; see
"Upgrading") and the first run's setup report (`<account_id>/setup-report.md`). Raw Rohlik responses, addresses and
payment data are never stored.

Optional `config.toml`:

```toml
stockup_min_discount = 0.35   # a sale qualifies for a stock-up from this marked discount
stockup_budget_czk = 1000     # the most a run spends on stock-up top-ups
```

To start over as a new user, delete the data directory and the keyring entries named `rohlik-replenish`.

## Upgrading

A new build may bring a newer database schema (`PRAGMA user_version`, 2 in this build). The first command of the new build that opens the database — `run`, but also `status`, `backtest` (without
`--csv`) or any `tags`/`prefs` command — upgrades it in place. The upgrade is **one-way**: an older build refuses a newer
database, and the engine never restores a copy. So, before the first command of a build with a newer schema:

1. With no command running, **copy the database**: `<data directory>/<account_id>/replenish.db`, on Windows
   `%LOCALAPPDATA%\rohlik-replenish\<account_id>\replenish.db` (macOS
   `~/Library/Application Support/rohlik-replenish/<account_id>/replenish.db`, Linux
   `~/.local/share/rohlik-replenish/<account_id>/replenish.db`). If a `replenish.db-journal` file sits next to it, an
   interrupted command left it: copy it too.
2. **Keep the older build.**

**To roll back,** put the copy back in place of `replenish.db` and reinstall the older build. What the newer build
recorded since the copy is lost: its runs and tag changes (cart lines it added then count as yours), and the orders it
synced, which the next run reads again.

**When the upgrade fills in choices for you to check,** make the first run a dry run
(`uv run replenish.py run --dry-run`), correct what its "Check these tags" lists (`tags …` with `--source user`, or
tell the agent), and only then run normally: a writing run would act on those choices.

## Development

Layout: `replenish.py` (entry point, PEP 723 header with bounded pins), `replenish.py.lock` (`uv lock --script`),
`replenish.requirements.txt` (the lock exported for tests), `engine/` (modules), `reference/`, `tools/`, `tests/`.

Tests run on the **locked** dependency versions:

```
uv run --with pytest --with-requirements replenish.requirements.txt pytest
```

(`--with-requirements replenish.py` would resolve versions afresh and ignore the lock.) `.python-version` pins the
test interpreter to 3.14, the newest version the lock is tested on. CI (`.github/workflows/tests.yml`) runs the suite
on Linux, macOS and Windows with Python 3.11 and 3.14.

When the dependencies change:

```
uv lock --script replenish.py
uv export --script replenish.py --locked -o replenish.requirements.txt
```

Fixtures: most tests build their households in code (`Scenario` in `tests/helpers.py`); the end-to-end CLI,
determinism and auth tests run on `tests/synthetic.py`, a made-up household in the `--fixtures` layout (its docstring
shows how to try the CLI on it without an account). The tests decorated with `@recorded` also check a real account's
history: anonymised read-only responses in `tests/fixtures/recorded/` (names, e-mails, addresses, phones, courier,
payment and delivery-note fields stripped; account and order IDs replaced) and a version-1 database made from them,
`tests/fixtures/v1/recorded.sql`. Being real order history, they are not in the repository, and those tests skip
without them. Record the responses after `login` with:

```
uv run --with-requirements replenish.requirements.txt python tools/record_fixtures.py tests/fixtures/recorded
```

Backtest on a CSV dataset laid out as in spec §15 (the maintainer's is real purchase history and stays outside the
repository; with `REPLENISH_BACKTEST_CSV` set to its folder, the tests also run their acceptance check on it):

```
uv run replenish.py backtest --csv path/to/data            # native units
uv run replenish.py backtest --csv path/to/data --raw-units
```
