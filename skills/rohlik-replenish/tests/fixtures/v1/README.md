# Version-1 databases

Real version-1 databases (`PRAGMA user_version = 1`) for the 1 → 2 migration tests (`tests/test_preferred.py`),
saved as SQL dumps (`sqlite3.Connection.iterdump()`). They were made by the last build with schema version 1,
through its own engine and commands, and are not hand-written:

- `recorded.sql`: the recorded household (`tests/fixtures/recorded`) through the CLI with `--fixtures`: `run`
  (`needs_tags`), `tags apply tags.json`, the first (dry) run and a writing run on 2026-10-26, then the user's
  corrections — `prefs never add "coconut milk"`, `tags set "oat drink" --stockup-mode on_sale --source user`,
  `tags set eggs --note … --source user`, a `tags apply --source user` that re-assigns a yogurt and moves an oat drink
  into a new user-created `solo` tag — and one more run (a checkout pending in fixture mode). It holds 19 `solo` tags.
- `scenario.sql`: a synthetic household (`tests.helpers.Scenario`) built for the fill rule: a `solo` tag decided by
  share (`eggs size l`: 5 of 9 against a more recent rival), one by a share tie (`coffee beans`: 2 : 2, the later
  purchase wins), one without a purchase in its window (`baby food jar`: the most recently bought), one whose SKUs were
  last bought together (`rice cakes`: the lower id), and one whose only SKU was never bought (`oat milk barista`); plus
  a never list, a user-set mode and source, runs, an edit, a checkout and its delivery.

The engine can no longer write version 1, so these files can't be regenerated. `recorded.sql` is real order history,
like `tests/fixtures/recorded`: it is not in the repository, and the tests that need it (marked `recorded`) skip
without it.
