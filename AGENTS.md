# Working in this repository

`rohlik-replenish` is an agent skill, packaged as a plugin for GitHub Copilot CLI and Claude Code, that fills a
Rohlik.cz cart from the user's order history. A deterministic Python engine does a run's Rohlik reads, arithmetic and
cart writes; `SKILL.md` tells the agent how to run it.

## Layout

- `docs/spec.md`: the specification and the source of truth. A change in behaviour starts there; code, tests and docs
  follow it. User-facing texts (§8, §9) are specified too, and the tests assert on them.
- `skills/rohlik-replenish/`: the skill. `SKILL.md`, `reference/`, the engine (`replenish.py`, `engine/`), `tests/`,
  `tools/`; its `README.md` covers commands, the data directory, upgrading and development.
- `.claude-plugin/`: `plugin.json` and a one-plugin `marketplace.json` (`"source": "./"`). Claude Code and Copilot
  CLI both read them.
- `.github/`: CI (`workflows/tests.yml`) and Dependabot for the SHA-pinned actions.

## Tests

From `skills/rohlik-replenish/`, on the locked dependency versions:

```
uv run --with pytest --with-requirements replenish.requirements.txt pytest
```

- CI runs them on Linux, macOS and Windows with Python 3.11 and 3.14, then starts the engine the way an installed
  plugin does (`uv run --locked replenish.py`), and checks that the lock matches `replenish.py` and the export matches
  the lock. Its `tests` job, which needs every other job, is the check `main` requires.
- `tests/conftest.py` gives every test a temporary data directory, an in-memory keyring and no browser, so no test
  can reach a real session. Keep it that way.
- Build test households in code: `Scenario` (`tests/helpers.py`), or `tests/synthetic.py` for the CLI end to end.
  Tests decorated with `@recorded` (`tests/helpers.py`) need the maintainer's private fixtures and skip without them;
  the maintainer runs the suite with them before merging a change to what users run.

## Rules

- **Never touch a real Rohlik account while developing.** Any command without `--fixtures DIR` talks to the account of
  whoever is logged in, and a non-dry `run` writes to its cart. Drive the engine with `--fixtures` (and `--now`), with
  `ROHLIK_REPLENISH_DATA_DIR` pointing at a scratch directory.
- **The tool allowlist is a safety boundary.** `engine/constants.py` allows six read tools and two cart writes; the
  engine never checks out, places, changes or cancels orders, reserves delivery slots or changes checkout settings.
  Don't widen it.
- **Never commit personal data.** These are real purchase history: `tests/fixtures/recorded/` and
  `tests/fixtures/v1/recorded.sql`, which stay git-ignored, and the backtest dataset of spec §15, which stays outside
  the repository (`REPLENISH_BACKTEST_CSV` points the tests at it).
- **Dependencies:** after changing the PEP 723 header of `replenish.py`, run `uv lock --script replenish.py` and
  `uv export --script replenish.py --locked -o replenish.requirements.txt`, and commit both files. To refresh them
  within the header's bounds, add `--upgrade` to the lock command, export again, run the tests, and release a patch.
  Dependabot can't update a script lock. If CI's `lockfiles` job fails only because a newer uv formats the export
  differently, export again and commit.
- **Database schema:** a change bumps `SCHEMA_VERSION` (`engine/constants.py`) with a one-way migration in
  `engine/db.py`, tested on a database of the previous version, and updates "Upgrading" in the skill's README.
- `SKILL.md`'s `name` must equal its folder's name, and the plugin's description in `plugin.json` and in the
  marketplace entry stay the same.

## Versions and releases

Users install `main`: the marketplace's source is the repository itself. So a commit that changes what users run
(`SKILL.md`, `reference/`, `replenish.py` and its lock, `.python-version`, `engine/`) is a release, and it bumps the
version. Claude Code offers an update only when `version` changes.

1. The version follows the spec: spec vX.Y is plugin X.Y.0. With a spec change, bump the version in `docs/spec.md`
   (title), the skill's `README.md` (first paragraph) and `.claude-plugin/plugin.json`; without one, bump only the
   patch in `plugin.json`. `tests/test_packaging.py` checks that they agree.
2. Once CI passes on that commit, run `gh release create vX.Y.Z --target <its SHA> --title vX.Y.Z --notes "…"`. Say
   in the notes when the release upgrades the database.
