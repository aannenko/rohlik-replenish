---
name: rohlik-replenish
description: Fill, prepare or restock the user's Rohlik.cz cart for the next delivery and reorder their usual groceries automatically from their order history. Use when the user asks to fill or top up the Rohlik cart, prepare the next Rohlik order, restock or reorder the usual groceries; when they correct how products are grouped ("milk 1.5 % and 3.5 % are different", "these two are the same thing"); and for standing preferences such as "never order X", "only buy X on sale", "always buy X" or "P is my preferred X".
---

# Rohlik replenishment

This skill fills the user's Rohlik cart for the next delivery **without asking what to buy**. A deterministic engine
(`replenish.py`) reads the user's Rohlik history through Rohlik's MCP server, learns how fast the household uses each
*product tag* (a need such as "milk 1.5%"), and adds what will run out before the delivery after next. The user
reviews the cart and checks out in the Rohlik app or website. The engine does all Rohlik reads, all arithmetic and
all cart writes; you run it, tag products when it asks, pass on corrections, and present its summary.

Run every command from this skill's folder (the folder that contains this `SKILL.md`).

## Rules

1. **The user has precedence.** Explicit instructions override these rules.
   - One-off requests ("I'm out of X", "skip Y this time", "add Z") are outside the engine. Carry them out yourself
     after the run, by editing the cart with Rohlik's tools; the engine treats those changes as the user's own.
   - Otherwise fill the cart autonomously: run the engine, and go.
2. Invoking a non-dry `run` is the user's authorization to change the cart as the engine plans.
3. You **must not**: check out, place, cancel or modify orders, reserve timeslots or change any checkout setting;
   touch the database or the data directory; compute quantities yourself; ask the user which products, quantities or
   variants to put in the cart (the plan is the engine's); change the cart on your own initiative. Outside a run you
   change the cart only when the user explicitly asks.
4. Allowed questions: reporting a failed stage and waiting for the user's choice; the one-line count-check reminder
   of step 3 (after a writing run only); and the tag check of step 4, which asks how products are grouped, not what to
   buy.

## Procedure

Steps 1–3 are stages: start each only after the previous one succeeded. When a stage fails, stop, tell the user which
stage failed and why, and suggest what to do. Carry out an action only when the user chooses it; never work around a
failure (no cart edits, no data-file edits).

### 1. Dependencies

Run `uv --version`. The minimum is **uv 0.5.20** (the oldest version that reads the committed `replenish.py.lock`;
0.5.19 cannot parse it, versions before 0.5.17 have no script locks).

If uv is missing or older, report it and suggest installing or updating it **user-locally, without administrator
rights**, with Astral's official installer:

- Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
- macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- An older uv: `uv self update`

Once the user agrees, run it and continue. If the current shell doesn't see `uv` yet, call it by its full path:
`%USERPROFILE%\.local\bin\uv.exe` on Windows, `~/.local/bin/uv` on macOS/Linux.

Python and the locked packages need no stage of their own: `uv run` provisions them on first use.

### 2. Run

`uv run replenish.py run` — add `--date YYYY-MM-DD` when the user names a delivery day, and `--dry-run` when they
only want to see what would be ordered. The engine prints one JSON envelope on stdout:
`{"status", "summary_md", "data", "warnings"}`.

Allow for the runtime: the **very first command on a machine** also waits for uv to download Python and the
packages, and the **first run** syncs 12 months of orders and looks up a few hundred products (up to ~3 minutes);
later runs take under a minute. Use a timeout of at least 10 minutes for the first command.

**The first run is always a dry setup run:** it writes nothing to the cart, and its summary is a compact setup report
that ends with the tag check (step 4). The cart is filled by the next run.

Handle the status:

- `ok` / `dry_run` → step 3.
- `auth_required` → run `uv run replenish.py login` so that its output reaches the user while it waits (it opens
  the browser and prints the login URL on stderr; it waits up to 5 minutes). Tell the user to finish logging in in
  the browser — this is routine, not an error. Then run step 2 again with the same options.
- `needs_tags` → tag **every product in `data.skus`** per `reference/tagging.md` (the summary names only the first
  10), write the JSON file, run `uv run replenish.py tags apply <file>`, fix any validation errors it reports, then
  run step 2 again with the same options. It can come on **any run**, whenever a new product appears in an order or
  the cart — not only on the first one.
- `insufficient_history` → present the summary and stop.
- `error` → report the failed stage (`data.stage`), whether the cart was changed (`data.wrote`) and the suggested
  actions (`data.actions`). Don't improvise a workaround.
- No JSON envelope on stdout → uv failed before the engine started (for example while downloading without a
  network): report uv's error output and suggest checking the connection and retrying.

### 3. Present

Present `summary_md` **verbatim**: no recomputation, reordering or omission, and at most two sentences of your own.
It is short by design: a writing run doesn't list the lines it added (the cart shows them), only top-ups bought on
sale, suggestions, reminders (paused tags, a `solo` tag buying without its preferred product, …), tags to check,
warnings and totals. When the user asks "why X?"
or "what did you add?", answer from `data`: `data.plan` has every line with its `why`, `reason`, quantities and
`fallback_from` (a `solo` tag buying without its preferred product), `data.writes` what was written, and
`data.kept_unchecked` the tags left as they were because Rohlik returned no product details.

When `data.report_path` is set (the first run), also give the user that file as a link: it is the full setup report;
the summary is its compact version.

After a **writing run** (`data.dry_run` is false), ask the user, **in one line**, to check each item's count in the
cart before checking out — a changed count is how the skill learns their amounts. After a dry run the cart is
unchanged: skip that line.

### 4. Tag check

When the summary ends with **"Check these tags"** (the first run's setup report with its guesses, products the agent
tagged since the previous run, or the `solo` tags whose preferred products a database upgrade chose), that is the
run's one question. Apply each correction the user gives with `uv run replenish.py tags …` and `--source user`
(step 5). If he doesn't answer, the tags stay as they are: don't ask again and don't change them.

After the first run (a dry setup run), once the user has answered its tag check or tells you to go ahead, **run step 2
again with the same options**: that run fills the cart.

### 5. Corrections (any time)

- Tag corrections → `uv run replenish.py tags …` with `--source user` (`tags rename`, `tags merge`, `tags set`,
  `tags apply` to move a product, change its storage tag or ignore it).
- Pack-size suggestions ("pack size unknown", "size in another unit") → `tags apply` with `units_per_pack`
  (`--source user` when the user gave the size).
- "Count X in pieces" / "count X by weight" (a tag with weighed products, ⚖) →
  `uv run replenish.py tags set X --base-unit pcs|kg --source user`. After a switch to pieces, give a weighed bunch or
  pack its piece count with `tags apply` (`units_per_pack`); the engine's warning names the products concerned.
- A standing preference for a product — "P is my preferred X", "always buy P for X", "from now on P", "make P solo" →
  `uv run replenish.py tags set X --preferred <P's product ID> --source user` (the ID is in `tags list --tag X`), which
  makes X `solo`. For a `variety` or
  `assorted` X, tell the user that its mix ends. When P is in another tag, move it into X first with `tags apply
  --source user`. When P has never been in an order or the cart, the engine doesn't know it yet: say that the choice can
  be made once it has been, and offer to add it to this cart. A one-off ("buy P this time") is a cart edit (rule 1),
  not a preference.
- "Never order X" → `uv run replenish.py prefs never add X`.
- "Only buy X on sale" / "always buy X" → `uv run replenish.py tags set X --stockup-mode on_sale|always --source user`
  for a long-life tag. For a short-life tag, explain that it is bought one delivery at a time and can't wait for a
  sale.
- "We order every N days" → `uv run replenish.py prefs cadence N` (`auto` to undo).

Show the result with `uv run replenish.py tags list --tag <tag>`.

Other commands: `status` (history, cadence, owned lines, locks, pauses, reminders, recent precision), `backtest` (how
well the model would have predicted the user's own history).
