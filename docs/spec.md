# Rohlik replenishment skill — specification v1.18

Source of truth for the implementation. An implementer needs only this document plus the dataset and reference script of §15, which hold real purchase history and are kept outside the repository.

**Conventions.**
- MUST / MUST NOT / SHOULD are used as in RFC 2119.
- *Engine* = the deterministic Python program.
- *Agent* = the LLM following `SKILL.md`.
- Dates are local calendar dates in Europe/Prague.

## 1. Purpose and scope

1.1 On demand, the skill fills the user's Rohlik cart for the next delivery **autonomously**, without asking what to buy (§10.3):
- (a) **Short-life tags** (perishables): a tag the user buys regularly is added when the stock the engine estimates is at home won't last until the **next** delivery, and only enough to last until then, which is one cycle. Nothing is bought further ahead. The tag's policy picks the product (§6.13);
- (b) **Long-life tags** (every product keeps, i.e. stock-up-eligible storage) have a stock-up mode (§6.14):
  - `always`: when the tag runs low (the same due test as (a)), the user's **usual batch** — their average purchase over the last 12 months — is added on top of what is left. Sales don't change the quantity;
  - `on_sale`: on a sale that Rohlik marks at least 35 % off, the tag is topped up to the user's **largest** purchase of the last 12 months (MAX), once per sale; everything it buys is on sale. When it runs low without such a sale, it is only shown as "running low — waiting for a sale"; nothing is added;
- (c) **every placed order is the user's decision**, whoever filled the cart: the user approves the cart at checkout. Rates and levels therefore learn from every order, and the user corrects a wrong quantity simply by changing it in the cart (§6.8, §6.10); the agent asks them to check the counts on every run (§10.2);
- (d) each long-life tag's mode is guessed once, on the first run, from the prices the user paid, and shown to the user to confirm or correct; later tags default to `always` (§6.14);
- (e) tags the user has stopped wanting — a line the agent added and the user removed twice — stop being proposed (§6.12).

1.2 The user reviews the cart and checks out in the Rohlik app or website. The skill never completes an order; Rohlik also requires orders to be completed only in the e-shop UI.

1.3 **The user has precedence.** The user's explicit instructions override this skill's rules. The engine doesn't model one-off requests such as "I'm out of X", "skip Y this time" or "add Z". The agent carries them out itself, outside the engine, typically by editing the cart after a run. The engine then treats those cart changes as the user's own (§6.15).

1.4 Out of scope for v1:
- checkout, payment and placing orders;
- timeslot reservation and every other checkout setting;
- cancelling or modifying placed orders, including adding the cart to an already placed order (Rohlik's "doobjednávka"; §6.6);
- per-run overrides;
- products the user has never bought;
- multi-buy offers;
- seasonality, vacations and party orders;
- an irregular-buyer guard and an order-share threshold;
- price-spike and quality flags;
- suggest-only ("manual") tags;
- substitution guards beyond tags;
- meal planning;
- scheduled runs.

## 2. Glossary

| Term | Definition |
| --- | --- |
| SKU | A Rohlik product ID (an integer; JSON maps from Rohlik use string keys). |
| Product tag | A household *need*, e.g. "milk 1.5%". It has an immutable `tag_id`, a display name, a policy and a base unit. SKUs with the same product tag are interchangeable for the household; a SKU with no siblings is a tag of one. All modelling is per product tag, and an unqualified "tag" means a product tag. |
| Storage tag | How a SKU is stored. It comes from a fixed, seeded set: `bakery`, `fresh-produce`, `chilled`, `fresh-meat-fish`, `frozen`, `dry`, `drinks`, `drugstore-household`, `pet`, `other`. Each storage tag has one attribute, `stockup_eligible`: whether its products keep long enough to stock up (seeds in §11, editable by the user). It is set per SKU, so fresh and frozen variants of one product tag can differ. Fresh dairy is `chilled`; UHT milk is `dry`. |
| Long-life / short-life tag | A product tag is **long-life** when every one of its SKUs has a `stockup_eligible` storage tag, else **short-life**. Long-life tags have a stock-up mode and use a 12-month window; short-life tags are bought one cycle at a time and use a 6-month window (§6.0, §6.14). A SKU stored unlike the rest of its tag is a sign of a wrong merge. |
| Stock-up mode | Per long-life product tag: `always` (when it runs low, the usual batch is added at whatever price; sales don't matter) or `on_sale` (topped up to MAX on a qualifying sale, once per sale; when it runs low without one, only a "running low" suggestion). Short-life tags have none. Guessed from prices on the first run for the user to confirm; later tags default to `always`; editable (§6.14). |
| Tag classes | Product and storage. Every SKU that isn't ignored has exactly one tag of each class. A new class is added only when a rule uses it. |
| Ignored SKU | A line that isn't a product, such as a promo catalogue, deposit or gift. It is never modelled. |
| Policy | Per product tag (§6.13), one name backed by one allocation routine = **breadth** × **order**. breadth is *single* (all units on the first candidate in the order) or *spread* (one unit at a time to the candidate furthest below its share: equal shares for `assorted`, the recent mix for `variety`); order is *cheapest* (price per base unit), *preferred* (the tag's preferred product first) or *proportional* (the recent mix). The four presets: `cheapest` = single × cheapest; `solo` = single × preferred (the cheapest when the preferred product is unavailable); `assorted` = spread × cheapest; `variety` = spread × proportional. The amount to buy is decided separately (§6.10) and fed in as a base quantity. |
| Preferred product | The one product a `solo` tag buys, whatever else the user buys now and then, and the cheapest available one only when it is unavailable (§6.13). The first run guesses it, the agent sets it when tagging, the user changes it on request; nothing else moves it (§5.3). |
| Variety tag, type | A product tag with policy `variety`, for products the household eats from one shared slot, such as sliced cheeses; its SKUs are its *types*. `assorted` likewise spreads across its SKUs. The tag's pace limits their total. |
| Base unit | Per product tag: `kg`, `l`, `pcs`, or `m` (metres) for products Rohlik prices per metre, such as toilet paper. |
| Size | Base units in one sale unit of a SKU: a 500 g pack in a `kg` tag is 0.5; a weighed product sold per piece is its kg per piece in a `kg` tag, and 1 in a `pcs` tag. Resolved per §6.2, and may be unknown. |
| Event | The lines of one tag in one placed order (§6.3). |
| Window | The 12 months before the evaluation date for a long-life tag, the 6 months for a short-life tag (§6.0). |
| n | The number of delivered events of a tag in its window with a positive base quantity. |
| C, T, H | C is the cadence (§6.4), T the delivery date of the order being prepared (§6.5), H the horizon (§6.10). |
| Agent-only / user-initiated | The origin of an event (§6.7). Only user-initiated events reset the projection (§6.9); every event counts for the rate and the levels. |
| Usual batch, MAX | Over a long-life tag's window events with a positive quantity, whatever their origin: the **usual batch** is their average base quantity (used by `always`), **MAX** the largest (used by `on_sale`) (§6.10). |
| Qualifying sale; waiting | A sale **counts** when it ends after today or has no end date, and **qualifies** when Rohlik's marked discount is at least 35 %. Once an order with a top-up of a tag has been placed, the tag is **waiting** until that top-up's sale ends, and gets no new top-up meanwhile (§6.14). |
| Choice event | A delivered event in which the user chose the tag's contents (§6.7). A `variety` tag's mix comes only from these. |
| Cycle | The runs between two placed orders (§6.6). |
| Run state | `dry`; `writing` (persisted by the write-ahead commit, before the first cart write); `done`; or `failed` (failed before its write-ahead commit, kept for audit only). |
| One-time notice | A summary line tied to a date d: a tag dropping out (§6.11) or a type leaving a `variety` mix (§6.13). It is shown only when d is after the date setup completed, by every run that starts on or after d, until a non-dry run that started on or after d has finished normally (`done` with `finished_at` set; a recovered crash doesn't count). That run shows it too. Nothing is stored for it. |
| Reminder | Like a one-time notice, but tied to a moment m and shown until 3 non-dry runs that started after m have finished normally: a paused tag and a type dropped from a `variety` mix (m = the placement of the order with the second removal; for an all-dropped `variety` tag, its latest drop), an `overdue` suggestion (m = the start of the day it became overdue), and a `solo` tag that couldn't buy its preferred product (m = the start of the first run of that streak, §6.13; that run counts, and so do only the runs that bought the tag without its preferred product or found it `unavailable` for want of it, so that a run that didn't buy the tag doesn't use up a showing it never showed). A moment, not a day, so that a run earlier on the day of the pausing order doesn't use up a showing it couldn't show. As with one-time notices, nothing whose moment falls on or before the setup day is shown, except a `solo` tag's fallback, whose moment is a run, never old history. `status` always lists them. |
| Reason code | Why a plan row exists (§6.11). |
| Miss, paused | A **miss** is a line the agent added that the user removed before the order was placed; two misses **pause** the tag (§6.12). |
| Locked | A tag with an edit detected in the current cycle: a change to a line the agent owned, or a change, just before writing, to any line of a tag the run was about to write (§6.15). |

## 3. Architecture

3.1 Components:
- **`SKILL.md` and `reference/tagging.md`:** instructions for the agent (§10).
- **Engine:**
  - Python ≥ 3.11, with a modular engine next to the entry script.
  - Entry script `replenish.py` with a PEP 723 header that pins **bounded** versions of its dependencies: the official MCP Python SDK `mcp`, `keyring` and `platformdirs`. SQLite comes from the standard library's `sqlite3`.
  - `requires-python` is `>=3.11` with an upper bound at the newest version the lock was tested on. On a newer system Python, uv then provisions a managed one instead of building packages from source.
  - A committed `uv lock --script` lockfile.
  - Run as `uv run replenish.py <command>`; there is no build step and no `pyproject.toml`.
- **Data directory:** `platformdirs.user_data_dir("rohlik-replenish", appauthor=False)` holds:
  - `auth.json`: the OAuth client registration (client ID, redirect URI, port, auth method), the authorization server's public metadata (at least its token endpoint, §4.2), whether a refresh token was issued, and the last account ID. Never secrets;
  - `lock`: the OS-level exclusive lock (§5.1);
  - `config.toml`: optional preferences (§11);
  - `<account_id>/replenish.db`: one SQLite database per Rohlik account, where `account_id` = `get_user_info().id`. It is opened once the account is known (§7.1).
  - `<account_id>/setup-report.md`: the first run's full setup report (§8.2), overwritten if setup runs again.

3.2 Responsibilities:
- The **engine** does:
  - all Rohlik I/O of a run, as its own MCP client (§4);
  - all parsing, including pack sizes;
  - all computation;
  - all cart writes of a run;
  - the ledger and the rendering of the summary.
- The **agent**:
  - runs engine commands;
  - gives the SKUs the engine lists a product tag (with its policy) and a storage tag, and resolves pack sizes the engine couldn't parse;
  - turns the user's tag corrections and standing preferences into engine commands;
  - presents the engine's summary verbatim.
- The agent MUST NOT do the engine's arithmetic or change the cart on its own initiative. It changes the cart only when the user explicitly asks (§1.3).

3.3 **Derived state is never persisted.**
- Rates, balances, misses, pauses, dropped types, mixes, levels, eligibility and parsed pack sizes are recomputed every run. The stock-up mode is a stored tag attribute: set by the first run's assessment, by the `always` default afterwards, or by the user (§6.14). The inputs are the database's durable tables (§5), config, the current Rohlik reads, and `now`.
- Identical inputs MUST produce byte-identical plan JSON. Every query used for planning has an explicit `ORDER BY`, and sorting is stable: by tag name, then `tag_id`, then SKU.

3.4 Raw tool responses are never persisted, because they contain address and payment data; only the normalised records of §5 are stored. Standard output is UTF-8 regardless of the console code page.

## 4. Rohlik MCP interface

4.1 Endpoint `https://mcp.rohlik.cz/mcp`, MCP Streamable HTTP transport. Every call passes the constant, non-personal `context` = "Automated grocery replenishment for the account owner."

4.2 Authentication
- **Discovery** (verified 2026-10-06):
    - Protected Resource Metadata at `https://mcp.rohlik.cz/.well-known/oauth-protected-resource/mcp` points to the authorization server `https://identity.rohlik.cz`, with scopes `openid email roles`.
    - The server metadata advertises dynamic client registration (`/connect/register`) and PKCE `S256`.
    - It offers client authentication `none`, `client_secret_basic` and `client_secret_post`.
    - Its grants are `authorization_code`, `refresh_token` and device code (`/oauth2/device_authorization`).
    - It appears to be Spring Authorization Server. Its defaults issue **no refresh token to public clients** on the authorization-code grant, and use 5-minute access tokens and 60-minute refresh tokens.
- **Login strategy**, resolved in V2 in this order:
    1. dynamic registration as a **confidential** client (`client_secret_basic` or `client_secret_post`), using authorization code + PKCE and a fixed, persisted loopback redirect `http://127.0.0.1:<port>/callback` (re-register if the port is busy);
    2. the device authorization grant (RFC 8628; the SDK has no provider for it).
  Use the first that works, and record whether a refresh token is issued and the token lifetimes.
  *Resolved 2026-10-26 (V2): strategy 1 works. Rohlik registers the client as `client_secret_post` even when `client_secret_basic` is requested, issues a refresh token, and access tokens last 8 hours (28,799 s); the refresh-token lifetime isn't reported. The 5-minute default above doesn't apply.*
- **Session policy (decided):** whenever no valid session exists, the user logs in again in the browser via `login`. This is routine, not an error.
- **`login`:**
    - opens the system browser at the authorization URL, or shows the device user code;
    - also prints it to stderr, flushed;
    - gives up after 5 minutes.
- Every other command MUST NOT start an interactive flow: the SDK's redirect handler raises an `AuthRequired` error, and the command returns status `auth_required`.
- Without a refresh token, a run checks the access token's remaining lifetime before its write phase. If fewer than 60 s remain, it returns `auth_required` without writing anything.
- **Refresh in a new process.** Every command is a new process, so a refresh must not depend on discovery made in that process. The SDK (mcp 2.3) sends a refresh to `<MCP origin>/token` unless the same process discovered the authorization server's metadata, and `https://mcp.rohlik.cz/token` answers 401; Rohlik's token endpoint is `https://identity.rohlik.cz/oauth2/token`. So `login` stores the discovered metadata in `auth.json`, and every command restores it before its first call. A session stored without it gets `auth_required` once, and the next `login` stores it. *Confirmed live on 2026-10-31: a new process refreshed silently 54 h after login.*
- **Storage:**
    - the refresh token, the access token with its expiry, and the client secret are **separate** OS-keyring entries under fixed names. Windows Credential Manager limits one secret to 2,560 bytes, so a longer token is split across numbered entries;
    - the access token is persisted because `login` and `run` are separate processes: without a refresh token, an in-memory token would be lost when `login` exits;
    - a refresh answer without a new refresh token keeps the stored one, since the server may reuse it (RFC 6749 §6); only a rejected refresh ends the session;
    - the registration and the authorization server's metadata are kept in `auth.json`.
  Secrets never go to files or logs. Legacy email/password headers MUST NOT be used.
- The SDK's 2.x client API differs from 1.x: the OAuth provider is an HTTP-client auth object, and the transport takes an `http_client`. Pin the major version the implementation is written against.

4.3 Tool allowlist, enforced in the client wrapper; any other tool raises before anything is sent. Confirm the server-side names with `tools/list` (V1).
- **Read:** `fetch_orders` (order types `delivered`, `upcoming` and `cancelled`), `get_product_details`, `get_cart`, `get_timeslots_checkout`, `get_user_info`, `get_checkout_link`.
- **Write:** `add_items_to_cart` and `update_cart_item`, only as §6.15 prescribes; a removal sets the quantity to 0.
- **Everything else is forbidden**, explicitly including:
  - `clear_cart`;
  - every checkout mutation (timeslot, suborder, packaging, payment, notes);
  - `cancel_order` and `remove_order_items`;
  - claims, shopping lists, favourites and scheduled tasks.

4.4 Calls and results
- **Calls:** sequential, with a 30 s timeout. All of a run's reads share a 10-minute budget: a read that would start after it fails the run at its stage, so an outage can't hold the lock for hours.
    - Read calls retry up to 3 times with exponential backoff on network errors, 429 and 5xx.
    - Write calls are never retried automatically. After an error or timeout the engine re-reads the cart and converges (§6.15).
- **Failures:** a tool failure may arrive as an exception or as a result with `is_error = true` (MCP SDK 2.x). Both are failures: either a read failure, or a write error handled by §6.15.
- **Parsing:**
    - Prefer `structuredContent`. Otherwise parse **only the first JSON value** of the first text block (`json.JSONDecoder().raw_decode`). Rohlik's text content repeats the payload, wrapped as `{"result": …}`, after the first value.
    - Unwrap a lone `{"result": …}`.
- **Envelopes differ per tool** (verified 2026-10-07):
    - `fetch_orders`: `{"success": true, "orders": [...]}`; its empty answer is `{"success": true, "data": null, "is_empty": true}`;
    - `get_cart`: `{"success": true, "data": {...}}`;
    - `get_product_details`: a bare map keyed by product ID, with no `success`;
    - `get_timeslots_checkout`: `{"data": {"availabilityDays": …}}`, with no `success`.
  An explicit `success: false`, or a payload where the tool's expected key is missing, fails that call and reports the tool name and message. A missing `success` key alone is not a failure. A SKU that `add_items_to_cart` lists in `items_failed_to_add` is a failed write, even with `success: true` (verified 2026-10-26). A `get_cart` answer whose `data.items` isn't a map, or with a line that lacks a finite, non-negative `quantity`, fails the read: an unreadable quantity is never taken as 0.
- **Per-item gaps never fail a run.** A product without prices or stock, or an ID missing from a product-details response, becomes a non-candidate with "no live data". A product-details batch that fails with a tool error is retried one ID at a time; one that still fails on transport, 429 or 5xx after its retries fails the read, and so does a read in which no requested ID returns details (§7.1 step 8). Unknown extra fields are only logged.

4.5 Known facts, from live read-only calls on 2026-10-07 (marked *verified*) and earlier recordings; open items are listed in §14.
- **`fetch_orders(order_type, date_from, date_to, limit, order_id)`:**
    - At least one of `order_id`, `limit`, `date_from` and `date_to` is required; `date_to` alone means a 6-month lookback, and `date_from` may not equal `date_to`.
    - *Verified:*
        - At most 15 orders come back per call.
        - A call with only `limit` returns the newest 15, **newest first**.
        - A date range matching more than 15 returns the **oldest 15, oldest first**, and the newest are silently missing.
        - **`date_to` is exclusive**: [29 Nov, 30 Nov) is empty, while [30 Nov, 1 Dec) returns an order delivered on 30 Nov.
    Always pass `limit=15`.
    - **Never use `order_type="both"`**: it hid upcoming orders.
    - *Verified:* `order_type="cancelled"` lists cancelled orders of all time, with `id`, `orderTime`, `itemsCount`, `state: "CANCELLED"` and totals only: no items and no delivery slot.
- **Delivered orders** contain (*verified*):
    - `id`;
    - `orderTime`, e.g. `2026-07-06T15:09:36.000+0200`. The offset has no colon and follows DST (+0100 in winter);
    - `deliverySlot{type, timeRange: "YYYY-MM-DD HH:MM-HH:MM"}`, whose first date is the delivery date. It can be the day after `orderTime`;
    - `priceComposition{goods, total, creditsUsed, delivery, courierTip, fines, …}`. `total` is what was paid **after credits** and can be a few percent of `goods`;
    - `items[]` with `id`, `name`, `quantity` (present since late August 2026), `unit` (**unreliable**: `kg`, `l` or `piece`, e.g. `kg` for packs), `totalPrice` and `textualAmount`;
    - `warrantyInfo.enabledData[]` and `warrantyInfo.disabledData[]`. Together they list every product with `productId`, `weighted`, `totalWeight` (kg, or null), `pieces`, `alreadyClaimedPieces` and `remainingPieces`, plus `pricePerPiece` and `total` as strings ("119.9 CZK");
    - personal fields such as `address`, which are never stored or logged.
- **Weighed products are sold in pieces.**
    - `quantity` is a whole piece count (all 218 weighed lines measured), and the actual weight is `totalWeight`.
    - An order line's `textualAmount` is the weight per piece **of that delivery**, and it varies between orders: 28 of 70 weighed SKUs show several values, e.g. a lemon at 180 g and at 300 g.
    - Product details give the current nominal size, e.g. "cca 150 g" (verified: 10.64 CZK ÷ `unitPrice` 70.9 = 0.150 kg).
- **Duplicate lines:** a SKU can appear more than once in one order, e.g. a zero-price compensation line beside the paid one. Their quantities add up.
- **Upcoming orders:**
    - `items[]` with `amount` (exact sale units) and `priceComposition`;
    - per Rohlik's tool documentation, `deliverySlot.timeRange` and `deliveryAnnouncements[]` with `isDelayed`;
    - whether they carry `orderTime` is unverified (V5).
  "Nothing on the way" is `{"success": true, "data": null, "is_empty": true}`, a valid answer and not an error (*verified*).
- **`get_product_details(product_ids, include_description=false)`** (*verified*):
    - It returns a bare map keyed by product ID. `include_description` defaults to true, so pass false.
    - Per product: `name`, `brand`, `textualAmount` and `countriesOfOrigin`, plus:
    - `prices{originalPrice, salePrice, unitPrice, saleId, saleValidTill, saleText, currency}`:
        - `originalPrice` can be null even on sale (7 of 163 recorded sale entries);
        - `salePrice` is null when there is no sale;
        - `unitPrice` is per kg or l, after any sale;
        - `saleValidTill` is often null: small standing discounts such as −10 % on milk carry no end date. It can also lie months or years ahead;
    - `stock{availabilityStatus, maxAvailableAmount, availabilityReason, maxBasketAmountReason, expectedReplenishment}`.
        - `maxAvailableAmount` is usually 50, but can be as low as 4.
        - Statuses seen: `AVAILABLE`; `UNAVAILABLE` (sold out); and `PREORDER`, e.g. bread baked for a later day, where `expectedReplenishment` is a timestamp. The field is absent on AVAILABLE products.
    - Some entries have no `stock` (6 of 465 recorded).
    - There are no category, weighed-flag or multi-buy fields. Since 2026-10-26 the tool's description mentions `xtraMembershipRequired` and `prices.xtraMemberPrice` (an Xtra-only offer the user isn't getting); the engine ignores both.
- **`get_cart`** (*verified*):
    - `data.items` is a map keyed by product ID (`{}` when empty). Each line has `productId`, `quantity` (sale units), `textualAmount`, `maxBasketAmount` and `maxBasketAmountReason`.
    - `data` also carries the cart total `totalPrice`, `totalSavings`, `minimalStandardOrderPrice` (749), `minimalDeliveryPointOrderPrice`, `minimalOrderPrice` and `submitConditionPassed`.
    - The cart under-reports discounts, so **prices come only from product details**.
- **Cart writes:**
    - `add_items_to_cart(items=[{productId, quantity, actionId?}])` adds to an existing line. `actionId` is a sale ID, resolved automatically when omitted;
    - `update_cart_item(product_id, quantity: integer ≥ 0)` sets the quantity, and 0 removes the line. It is documented as changing "a product already in the cart" and can't create an absent line (V4);
    - `remove_cart_item(product_id)` (not used: a removal sets the quantity to 0).
- **`get_timeslots_checkout(timeslots_day ∈ "0".."3")`** (*verified*): `data.availabilityDays{date, slots{"<hour>": [{slotId, timeWindow, slotType, capacityPercent, price, …}]}}`. Available slots have a `slotId`; full slots show `capacityPercent: "NO CAPACITY"` and have none. Late in the day, day "0" answered with the tool's usage hint as its message and no `availabilityDays` (live, 2026-11-01 20:04), i.e. as a failed read.
- **`get_user_info`:** only `id` and `xtra.limits.orders_without_min_remaining` are used. Other profile fields are never stored or logged.
- **Cancelling an order** in the Rohlik app or tool puts its items back into the cart by default (`cancel_order`'s `readd_items_to_cart` defaults to true).

## 5. Data model and storage (SQLite)

5.1 Principles
- **One database per account**, `replenish.db`, through the standard library's `sqlite3`.
  - Every connection sets `PRAGMA foreign_keys = ON` **before** any transaction and asserts that it reads back 1. Python's `sqlite3` can open transactions implicitly, and the pragma does nothing inside one.
  - The journal is SQLite's default rollback journal, since one process at a time holds the lock.
- **Schema version** in `PRAGMA user_version` (= 2). Migrations are forward-only and run at startup, each step inside one transaction.
  - **1 → 2** (preferred products). SQLite can't add the new `product_tags` CHECKs to a table that holds `solo` rows, nor rebuild a parent table while foreign keys are on, and the pragma does nothing inside a transaction. So this step alone turns foreign keys off before its transaction and asserts it; rebuilds `product_tags` with the new columns and CHECKs, filling each `solo` tag's preferred product (§5.3); adds `run_plan.fallback_from`; requires `PRAGMA foreign_key_check` to return nothing before it commits; sets `user_version` = 2 and, if it set any preferred product, the meta key `preferred_check` (§8.1); then turns foreign keys back on and asserts it. A failure rolls everything back and leaves version 1.
- **Durable inputs only.**
  - No table or column holds a value that can be recomputed from the others: no rates, balances, misses, pauses, mixes, eligibility or parsed pack sizes. Mixing history with derived state lets a stale derived value silently rewrite history.
  - Upcoming-order and cart observations, and detected edits, are inputs: they can't be re-read later.
- **Nothing is pruned.** Expected growth is about 20 k rows a year. History, runs and preferences are kept for the life of the database.
- **Every change goes through engine commands.** Neither the user nor the agent runs SQL, and tests drive the engine's API against an in-memory database, never raw inserts.
- **Formats:** timestamps are ISO 8601 text with an offset, and dates are `YYYY-MM-DD` text. Raw tool responses are never stored.
- **The lock** is `<data>/lock`: an OS-level exclusive lock on an open handle (`msvcrt.locking` / `fcntl.flock`).
  - It is held for the whole command and released automatically when the process dies.
  - A concurrent command fails fast with a clear message.

5.2 Schema (normative: names may change, constraints may not be weakened)
```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
  -- keys: setup_completed_at, cadence_days (the user's override), preferred_check (set by the 1 → 2 migration, §8.1)

CREATE TABLE storage_tags (                             -- the fixed set, seeded by the migration (§11);
  name             TEXT PRIMARY KEY,                    -- never created, renamed or deleted afterwards
  stockup_eligible INTEGER NOT NULL CHECK (stockup_eligible IN (0,1)),
  updated_at       TEXT NOT NULL);

CREATE TABLE product_tags (
  tag_id     TEXT PRIMARY KEY,                          -- engine-assigned, immutable
  name       TEXT NOT NULL UNIQUE CHECK (name = lower(name) AND length(name) BETWEEN 2 AND 64),
  policy     TEXT NOT NULL CHECK (policy IN ('cheapest','assorted','solo','variety')),
  base_unit  TEXT NOT NULL CHECK (base_unit IN ('kg','l','pcs','m')),
  stockup_mode TEXT CHECK (stockup_mode IN ('always','on_sale')),   -- long-life tags; NULL = no mode yet, planned as always (§6.14)
  note       TEXT,                                      -- the user's reason, e.g. why `solo` or `variety`
  source     TEXT NOT NULL CHECK (source IN ('agent','user')),
  preferred_sku    INTEGER REFERENCES skus(sku),      -- `solo` tags: the product the tag buys (§5.3, §6.13)
  preferred_source TEXT CHECK (preferred_source IN ('agent','user')),
  updated_at TEXT NOT NULL,
  CHECK ((policy = 'solo') = (preferred_sku IS NOT NULL)),
  CHECK ((preferred_sku IS NULL) = (preferred_source IS NULL)));

CREATE TABLE skus (                                     -- every SKU seen in orders, upcoming orders or the cart
  sku            INTEGER PRIMARY KEY,
  name           TEXT NOT NULL,
  textual_amount TEXT,                                  -- the latest text seen anywhere
  weighted       INTEGER NOT NULL DEFAULT 0 CHECK (weighted IN (0,1)),     -- from order lines
  ignored        INTEGER NOT NULL DEFAULT 0 CHECK (ignored IN (0,1)),
  product_tag_id TEXT REFERENCES product_tags(tag_id) ON DELETE RESTRICT,
  storage_tag    TEXT REFERENCES storage_tags(name) ON DELETE RESTRICT,
  assigned_by    TEXT CHECK (assigned_by IN ('engine','agent','user')),  -- NULL = waiting for tags
  upp_override   REAL CHECK (upp_override > 0),         -- a size given by the agent or user
  upp_unit       TEXT CHECK (upp_unit IN ('kg','l','pcs','m')),  -- the base unit upp_override was given in
  upp_source     TEXT CHECK (upp_source IN ('agent','user')),
  updated_at     TEXT NOT NULL,
  CHECK ((product_tag_id IS NULL) = (storage_tag IS NULL)),                -- both tags or neither
  CHECK (ignored = 0 OR product_tag_id IS NULL),                           -- ignored ⇒ untagged
  CHECK ((assigned_by IS NULL) = (ignored = 0 AND product_tag_id IS NULL)),
  CHECK (assigned_by <> 'engine' OR ignored = 1),                          -- the engine only auto-ignores
  CHECK ((upp_override IS NULL) = (upp_source IS NULL) AND (upp_override IS NULL) = (upp_unit IS NULL)));

CREATE TABLE orders (                                   -- delivered orders
  order_id      INTEGER PRIMARY KEY,
  order_time    TEXT NOT NULL,
  delivery_date TEXT NOT NULL,
  slot_end      TEXT,                                   -- end of the delivery slot; NULL when unknown (§6.9)
  total_czk     REAL,
  fetched_at    TEXT NOT NULL);

CREATE TABLE order_lines (                              -- one row per SKU per order; duplicate entries merged
  order_id         INTEGER NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
  sku              INTEGER NOT NULL REFERENCES skus(sku),
  quantity         REAL CHECK (quantity >= 0),                     -- sale units of all entries; NULL = unknown
  weight_kg        REAL CHECK (weight_kg >= 0),                    -- weighed SKUs: actual total weight
  textual_amount   TEXT,                                           -- this delivery's text (per piece if weighed)
  line_price_czk   REAL NOT NULL,
  claimed_quantity REAL NOT NULL DEFAULT 0 CHECK (claimed_quantity >= 0),
  PRIMARY KEY (order_id, sku));

CREATE TABLE cancelled_orders (                         -- orders seen upcoming and later listed as cancelled (§6.1)
  order_id INTEGER PRIMARY KEY,
  seen_at  TEXT NOT NULL);

CREATE TABLE prefs (                                    -- standing preferences on product tags
  tag_id TEXT NOT NULL REFERENCES product_tags(tag_id) ON DELETE CASCADE,
  kind   TEXT NOT NULL CHECK (kind IN ('never')),       -- `on_sale` is a tag attribute (stockup_mode), not a pref
  at     TEXT NOT NULL,
  PRIMARY KEY (tag_id, kind));

CREATE TABLE runs (
  run_id       TEXT PRIMARY KEY,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,                                    -- stays NULL for a run that crashed, also after its recovery
  state        TEXT NOT NULL CHECK (state IN ('dry','writing','done','failed')),
  recovered    INTEGER NOT NULL DEFAULT 0 CHECK (recovered IN (0,1)),     -- finalised by a later run (§6.15)
  t_date       TEXT,                                    -- NULL when the run failed before T was known
  cadence_days REAL,
  options      TEXT NOT NULL DEFAULT '{}',              -- JSON audit, e.g. {"date": "2026-10-09", "dry_run": false}
  summary_md   TEXT);

CREATE TABLE run_upcoming_orders (                      -- orders checked out but not yet delivered, as a run saw them
  run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  order_id      INTEGER NOT NULL,
  order_time    TEXT,                                   -- NULL when Rohlik doesn't report it
  delivery_date TEXT NOT NULL,
  slot_end      TEXT,                                   -- end of the delivery slot; NULL when unknown (§6.9)
  sku           INTEGER NOT NULL REFERENCES skus(sku),
  amount        REAL NOT NULL,
  PRIMARY KEY (run_id, order_id, sku));

CREATE TABLE run_plan (                                 -- one row per tag decision with an output line (§6.11);
  run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,   -- a `variety` or `assorted` tag has one per allocated SKU
  sku           INTEGER NOT NULL REFERENCES skus(sku),  -- the chosen SKU or type, else the tag's most recently bought
  reason        TEXT NOT NULL CHECK (reason IN ('due','suggest_gate','wait_sale','size_unknown',
                                                'basket_limit','unavailable','stockup','budget_limit','overdue')),
  normal_units  REAL NOT NULL DEFAULT 0 CHECK (normal_units >= 0),   -- the one-cycle need, or an `always` tag's usual batch
  stockup_units REAL NOT NULL DEFAULT 0 CHECK (stockup_units >= 0),  -- an `on_sale` top-up above the need (§6.10)
  sale_until    TEXT,                                   -- a top-up's sale end date; NULL without one (§6.14)
  size          REAL CHECK (size > 0),                  -- the size used, in size_unit
  size_unit     TEXT CHECK (size_unit IN ('kg','l','pcs','m')),
  fallback_from INTEGER REFERENCES skus(sku),          -- `solo`: the preferred SKU this row stands in for (§6.13)
  PRIMARY KEY (run_id, sku));

CREATE TABLE run_cart (                                 -- per-SKU cart snapshot: every SKU in the cart, planned,
  run_id            TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,   -- or agent-owned by the previous run
  sku               INTEGER NOT NULL REFERENCES skus(sku),
  before_qty        REAL NOT NULL,                      -- the cart at plan time
  user_owned_before REAL NOT NULL,
  target_qty        REAL,                               -- NULL = no write intended
  after_qty         REAL,                               -- set when the SKU's write is confirmed or it is skipped, else when the run is finalised (§6.15)
  agent_owned       REAL,                               -- set together with after_qty
  PRIMARY KEY (run_id, sku));

CREATE TABLE run_edits (                                -- edits this run detected (§6.15)
  run_id       TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  sku          INTEGER NOT NULL REFERENCES skus(sku),
  proposal_run TEXT NOT NULL REFERENCES runs(run_id),   -- the run that proposed the edited line (§6.15)
  PRIMARY KEY (run_id, sku));

CREATE TABLE run_calls (                                -- call log without payloads
  run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,
  tool        TEXT NOT NULL,
  args_json   TEXT NOT NULL,
  duration_ms INTEGER NOT NULL,
  outcome     TEXT NOT NULL,
  PRIMARY KEY (run_id, seq));
```

5.3 Rules the schema doesn't express
- **Tag identity.** A product tag's `tag_id` is assigned when the tag is created and never changes; its `name` may change. Plan rows, cart rows and edits reference SKUs, and a SKU's product tag is always resolved through the **current** mapping. So renames, merges and splits keep their history.
- **SKU states.** A SKU is in exactly one of three states:
    - waiting for tags (`assigned_by` is NULL);
    - ignored;
    - tagged, with one product tag and one storage tag.
  SKUs whose cached order lines are all zero-priced are ignored automatically (`assigned_by = 'engine'`), unless the agent or user has assigned them. Sync re-evaluates this: an engine-ignored SKU that gets a paid line goes back to waiting for tags.
- **Units.**
    - Every SKU of a product tag is measured in the tag's base unit.
    - A weighed SKU belongs to a `kg` or a `pcs` tag; `tags apply`, `tags merge` and `tags set` reject violations. In a `kg` tag it counts by its actual weight; in a `pcs` tag by its pieces, whatever they weigh (§6.2, §10.4).
    - `tags apply` also rejects a SKU whose text doesn't parse in its tag's base unit, whether it parses in another unit or not at all, unless the entry gives `units_per_pack`: a number in the tag's base unit, or an explicit null when it can't be determined. A weighed SKU in a `pcs` tag is exempt: its text is a weight, and its size is 1 piece unless `units_per_pack` gives its piece count, as for a weighed bunch of bananas.
    - A SKU whose text parses in a different unit has an unknown size (§6.2), except a weighed SKU in a `pcs` tag. Sync never fails on units.
- **Source.** Assignments and sizes with source `user` are never overwritten by the agent.
- **Storage tags.** They are the fixed seeded set; `tags storage-set` changes only their stock-up flag.
- **Stock-up mode.** NULL until the first run assesses the tag, a later run gives it `always` at n ≥ 3, or the agent or user sets it (§6.14). `tags set --stockup-mode`, and a `stockup_mode` in `tags apply`, are rejected for a tag that is short-life after the change; in `tags apply`, a mode equal to an existing tag's is ignored and a different one is an error (change it with `tags set`). A tag that turns short-life keeps its mode, unused, until it is long-life again. `tags merge` keeps INTO's mode, or takes FROM's when INTO has none.
- **Preferred product.** Exactly the `solo` tags have one: one of the tag's own tagged SKUs.
    - It is set by the first run's guess (§6.13), by `tags apply` with `preferred: true` (§7.3), or by `tags set --preferred`, which also makes the tag `solo`. Nothing else moves it: purchases, the cart and prices never do.
    - The agent names one only for a new `solo` tag. On an existing tag only the user changes it (`--source user`), so a guess he left in place doesn't drift with later purchases either.
    - One the user set is protected. Setting it with `--source user` also makes its SKU's assignment the user's (Source, above). An agent-source command whose end state would clear or change it — another policy on its tag, moving or ignoring its SKU, merging its tag away — is rejected. A user-source command may, and reports what it dropped. All of this is judged on the transaction's end state, not entry by entry.
    - Setting another policy clears it. When the preferred SKU leaves its tag (moved to another tag, or ignored), the tag becomes `cheapest`, reported as "<tag> now buys the cheapest: its preferred product moved; name another to keep it `solo`".
    - `tags merge` keeps INTO's policy and preferred product, except that a user-set one in FROM wins over an agent-set one in INTO. Every preferred product a merge drops is reported.
    - The 1 → 2 migration (§5.1) gives each `solo` tag its SKU with the largest share (§6.13, over the tag's window at the migration, without the price condition); a tag without a purchase in its window gets the SKU it bought most recently, then the lower SKU id. These count as the agent's, and the next run shows them for checking (§8.1).
- **Empty product tags.** A product tag left without SKUs is deleted in the same transaction; its preferences are dropped and reported. `tags merge` moves preferences to the surviving tag first, collapsing duplicates.

5.4 Transactions
- **Sync** is one transaction: orders, lines, new SKUs, latest texts and cancelled orders.
- **Every `tags …` and `prefs …` command** is one transaction. Any violation rolls back the whole change and reports precise errors.
- **Recovery** of a crashed run (§6.15) is one transaction, committed before planning.
- **A writing run** uses these transactions:
    1. **the write-ahead commit**, before the first cart write: the `runs` row with `state = 'writing'`, its plan, its cart snapshot with targets, its upcoming orders and its detected edits;
    2. right after the pre-write re-read (§6.15 write protocol step 2), before any write, one small transaction stores the skipped SKUs' `after_qty` and `agent_owned` and their `run_edits` rows;
    3. after each cart write that Rohlik confirms, a small transaction stores that SKU's `after_qty` (= its target) and `agent_owned` (§6.15), so a crash can't lose a write that reached the cart;
    4. at the end of the run (§7.1 step 13), one transaction stores `after_qty` and `agent_owned` of every row that has none yet (§6.15 write protocol step 5), the assessed stock-up modes (§6.14), `state = 'done'`, `finished_at` and `summary_md`.
  Any failure after the write-ahead commit — before, between or after the writes — leaves the run in `writing`, and the next run recovers it (§6.15).
- **A dry run** is stored with `state = 'dry'` in one transaction at the end, together with the assessed stock-up modes and the guessed preferred products (§6.13). For the first run, the same transaction sets `setup_completed_at`, so a first run that fails anywhere leaves setup incomplete.
- **A failed run** — one that fails before its write-ahead commit — is stored as `failed`, for audit only.
- Dry and failed runs never take part in cycles, ownership, origin or the replay (§6.6, §6.7, §6.12, §6.15). The exception is their `run_upcoming_orders` rows, which are observations like any other (§6.6).

## 6. Algorithms

Every computation runs at an evaluation time t, with evaluation date e (§6.0).

### 6.0 Time and numerics
- **Days and history.** Arithmetic is in whole days; e = the local date of t. **History** = the orders delivered before e (`delivery_date < e`). Every placed order outside history is incoming stock (§6.9).
- **Window.** Per tag, ending at e: **365 days** for a long-life tag, **183 days** for a short-life tag (see the glossary); a window event is a history event dated after e − W. The window bounds n, the rate (§6.8), the usual batch and MAX (§6.10), the assessment (§6.14), the gate and the tag's lifetime: a tag with no window event drops out until bought again. "The window" below means the tag's own window.
- **Rounding.** Comparisons use ε = 1e-9. Round up = `ceil(x − 1e-9)`, round down = `floor(x + 1e-9)`, and round = half up. C may be fractional, and so may a horizon: consumption is linear within a day.
- **Medians and percentiles.** A median of an even count is the mean of the two middle values. Percentiles use the nearest rank: rank ⌈p · n⌉, ascending.

### 6.1 Sync and coverage
- **Newest orders.**
  - Every run reads the newest delivered orders with `fetch_orders(order_type="delivered", limit=15)`; a `limit`-only call returns the newest.
  - It upserts them, picking up new orders and late claims.
  - A line's quantity is `items[].quantity`, else the warranty piece count, else unknown (NULL).
  - An order's slot end is the end of its `deliverySlot.timeRange`, else NULL; upcoming orders record it too.
- **Catching up.** If none of the newest orders is cached, the run pages **forward**, using the verified fact that a date range returns its oldest 15, oldest first.
  - The start date is the earlier of the newest cached order's order date and delivery date, since it's unverified which of them date ranges filter on (V7); for an empty cache it is e − 365 days. The first sync is this case.
  - Call `fetch_orders(order_type="delivered", date_from=start, date_to=e + 1 day, limit=15)`; `date_to` is exclusive.
  - Upsert the orders, deduplicated by ID.
  - While a page returns 15 orders, take as the next start the earlier of the last returned order's order date and delivery date. The overlap is harmless, since IDs are deduplicated.
  - Stop at a page with fewer than 15, or when a page meets the newest read.
  - A start that doesn't advance (15 orders on one day) fails the run.
- **Coverage.** Forward paging leaves no gaps, so there is no coverage warning and no gap heuristic: a long vacation is not a missing order.
- **Upcoming orders and cart.** Every run also reads upcoming orders (`order_type="upcoming"`, `limit=15`) and the cart. If either read fails, the run fails: "nobody looked" must never look like "nothing on the way".
- **Vanished orders.** These are orders recorded in some run's `run_upcoming_orders` that are no longer upcoming, aren't cached as delivered, and aren't in `cancelled_orders`.
  - If any exist, the run calls `fetch_orders(order_type="cancelled", limit=15)` once, and stores in `cancelled_orders` those vanished orders that it lists.
  - An order still unexplained keeps its last-seen lines as incoming stock (§6.9) while its last-seen delivery date is today or later, with the warning "order <id> is no longer listed". After that date it is dropped, with the warning "order <id> disappeared without being delivered or cancelled".
- **New SKUs** from order lines, upcoming orders or the cart are inserted as waiting for tags.
  - `skus.textual_amount` follows the latest text seen, whether from an order line, a cart line or product details.
  - `weighted` comes from order lines.

### 6.2 Sizes and base quantities — the input boundary
All quantities are normalised here. Later sections use only base quantities, sizes and the "unknown" marker.
- **parse(text)** follows the grammar of §10.4 and returns (amount, unit): `g`/`kg` become kg, `ml`/`l` become l, `ks` becomes pcs, and `m` (metres) stays m. Anything else parses to nothing.
- **size(s, text)**, for a SKU s of a tag with base unit u:
  - `upp_override`, if set;
  - else 1, for a weighed SKU in a `pcs` tag: one piece, whatever its weight and text;
  - else parse(text), when its unit is u;
  - else unknown. A parse in another unit makes the size unknown, reported as "size in another unit: <text>".
- **Current size** (used for sizing and current prices) = size(s, the live `textualAmount` from this run's product details), else size(s, `skus.textual_amount`).
- **Line base quantity** of a **delivered** line, including one delivered today:
  - a weighed line with `weight_kg`, in a `kg` tag: weight_kg × (1 − claimed ÷ quantity), or weight_kg alone when nothing was claimed;
  - otherwise, including every weighed line in a `pcs` tag, whose claims count in pieces: (quantity − claimed) × size(s, the line's own `textual_amount`, else `skus.textual_amount`);
  - unknown when a needed quantity or size is unknown, and never below 0.
- **Lines of undelivered orders and cart lines:** units × current size. Actual weights and claims exist only once a line is delivered.
- **Stored sizes carry their unit.** `upp_override` and `run_plan.size` are stored together with the base unit they were given in. If that unit differs from the tag's current base unit (after `tags set --base-unit` or a merge), the stored size counts as unknown; it is never reinterpreted.
- **Size-unknown tags.** A tag is size-unknown at e when any of its window events, incoming lines or cart lines has an unknown base quantity.

### 6.3 Events
- **Definition.** An event of tag g in a placed order O is O's lines of g:
  - date = O's delivery date;
  - placed = O's placement time (§6.6);
  - q = Σ line base quantities;
  - spend = Σ `line_price_czk`;
  - sku_units = {sku: quantity}.
- **q = 0.** An event with q = 0 enters no rate, balance or reset, but the order still *contains* g for §6.12.
- **n** = the number of the window's delivered events of g with q > 0.

### 6.4 Cadence C
The median of the last ≤ 8 gaps between consecutive distinct delivery dates in history, clamped to [4, 21] days. `meta.cadence_days` overrides it, and fewer than 2 gaps → 7.

### 6.5 Delivery date T
The date passed with `--date`, which must be ≥ today. Otherwise the earliest date with an available slot in `get_timeslots_checkout` for days 0..3, each day read on its own: a day whose read fails (§4.4) is skipped, not the end of the search, since a day without slots can answer that way (§4.5); a transport failure that outlasts its retries ends the search. Otherwise today + 1, with a warning that says which: "no free delivery slot in the next 4 days", or "couldn't read the delivery slots for N of the next 4 days".

### 6.6 Placed orders, cycles and matching
Cycles are resolved once per run. Ownership, origin, incoming stock and misses all use the same result.
- **Placed orders** = cached delivered orders ∪ current upcoming orders ∪ every order recorded in any run's `run_upcoming_orders`, each with its last-seen time, date and lines.
    - Orders in `cancelled_orders` count only as cycle boundaries: they add no stock and no events, and §6.12 skips them.
- **Placement time** = the order's `order_time`. When Rohlik doesn't report one (V5), it is inferred from the order's **first sighting**, the first run that recorded it. Orders without an `order_time` are resolved one at a time, by first sighting, then order ID. An order's **reference run** is the last non-dry run that owned SKUs the order contains (a crashed run as it provisionally owns, §6.15 step 1) and started before the order's first sighting, but after the placement of every other order placed by then: a known `order_time` before that sighting, or a placement inferred earlier in this pass. The placement time is the reference run's finish, or its start if it crashed, since a crashed run's `finished_at` stays NULL. Without a reference run, it is the start of the first sighting's run. A checkout seen late is then still placed before the runs that came after it, later runs can't move it, and no run is the reference run of two orders. If V5 shows that upcoming orders carry `orderTime`, this inference and the reference run can be deleted, with their tests (§14).
- **Cycles and matching.** A **cycle** is closed by a placed order. Orders are ordered by placement time, then order ID.
    - An order closes the cycle of the non-dry (`writing` or `done`) runs that started strictly before its placement time and at or after the previous closing order's placement time, plus its reference run, which always belongs to the cycle it closes. A run that would fall in two cycles belongs to the earlier one, so each run belongs to at most one closed cycle. A run that starts exactly when an order is placed, such as the run whose sighting fixed that placement, belongs to the next cycle.
    - It is **matched** to the last of those runs.
- **Checkout pending.**
    - **Condition:** the last non-dry run L owned at least 1 SKU, every SKU it owned has left the cart, the cart is otherwise empty, no order has closed L's cycle, and L finished less than 24 hours ago (counting from its start if it crashed).
    - **Then** the engine assumes a checkout that Rohlik hasn't published yet and stops after its reads: it plans nothing, detects no edits and writes nothing. The run is stored as `dry`, so L stays the reference until the order appears, and its summary is the banner "looks like you just checked out — re-run once the order shows up".
    - After 24 hours the rule lapses, and L's missing lines count as the user's edits (§6.15). This also covers a cart added to an already placed order, which is out of scope (§1.4): the merged items arrive with that order as incoming stock, their tags stay locked until the next order, and they may count as one miss.
    - V9 may remove this rule.
- **Checkout in progress.**
    - **Condition:** let L be the last non-dry run that started before the new order's placement. L owned at least 1 SKU; an upcoming order that no earlier run recorded (first sighted by this run) holds every SKU L owned, with at least the owned units; and the cart still shows them.
    - **Then** the run writes nothing (it is stored as `dry`) and says "checkout in progress — re-run shortly".
- **Unmatched orders** close no run's cycle: the user ordered without the skill.

### 6.7 Ownership at close, origin and written stock-up
- **Ownership at close** of a cycle whose last run is L: owned[s] = L's `agent_owned[s]`.
  - A SKU edited in the cycle (a `run_edits` row of one of its runs) instead takes the `agent_owned[s]` of the edit's proposal run, i.e. what the agent wrote before the edit.
  - The **owner run** of s is that proposal run for an edited SKU; otherwise it is the latest run of the cycle, up to L, with a `due` or `stockup` row for s. Every owned SKU has one, since a cycle starts with nothing owned, so a locked tag's untouched lines keep the run that proposed them.
  - **Offered** units of s = owned[s] when its owner run's row for s has reason `due` or `stockup`, else 0; their base quantity uses that row's size. Used by `variety` tags (§6.12, §6.13).
- **Origin** of an event of tag g in an order matched to a cycle:
  - it is **agent-only** iff, for every SKU s of g in the order, the order's units of s ≤ owned[s];
  - otherwise it is **user-initiated**;
  - events of unmatched orders are user-initiated.
- **Choice event** of g: a delivered event of g with q > 0 whose order is unmatched, or in which, for some SKU s that the order contains or the agent owned, the order's units of s differ from owned[s] (absent = 0). The user added, removed or changed something in g. Only a `variety` tag's mix uses it (§6.13): the agent's unchanged lines never count, so neither its own allocations nor its sold-out substitutions shift the mix. Accepted noise: a line Rohlik shorts at picking looks like the user's removal.
- **Written stock-up** of s = max(0, min(stockup_units[s], owned[s] − normal_units[s])) in sale units, where `stockup_units` and `normal_units` come from the owner run's plan row (0 without a row).
  - **Taken stock-up** of s in an order, when its owner run's row for s has stockup_units > 0, = max(0, the order's units of s − normal_units[s]); otherwise 0. Units the user kept count as normal first, so a deleted top-up never absorbs a kept need, and units the user added to a top-up line count as stock-up too: raising a top-up is how the user raises MAX (§6.10).
  - stockup_base of an event = Σ_s taken stock-up of s × (the line's base quantity ÷ its units). This is in actual base units, so weighed pieces in a `kg` tag use their real weight.
  - stockup_base is 0 for unmatched orders.

### 6.8 Rate
Consumption is learned from **every** placed order of the tag, whoever filled the cart: the user approves each order at checkout, so its final quantities are the household's decision (§1.1).
- **Interval [a, b):** a and b are the first and last dates of the tag's window events with q > 0, if they differ; else the two most recent distinct dates of its history events with q > 0, if there are two; else there is none.
- **r** = Σ q over the history events dated in [a, b) ÷ (b − a). The event at b isn't counted: it hasn't been eaten yet.
- **No rate** without an interval; the tag can't be due.
- **An accepted order is not "I ran out".** Only the user's own additions reset the projection (§6.9).
- Measured in closed loops (§13.3): resetting the projection on every order made an `always` tag over-buy by 23–38 %, with stock growing without bound. Never resetting it left the user correcting a tag on every order once his consumption changed; with resets, about 20 corrections sufficed.

### 6.9 Stock transitions — one routine for history, projection and backtest
- **State:** batches, each with an arrival date, an arrival time (for resets), a quantity and a stock-up flag.
- **Consumption:** each day d consumes r_d, the rate of §6.8 evaluated at d (from the events dated before d), oldest arrival first; on the same date, normal batches go before stock-up batches. A day before the tag's first rate consumes at that first rate, and the days from e on at today's r (§6.10). r_d changes only on the day after an event's date and on the day an event leaves the window, so a replay computes it only there.
  Replaying past days at today's rate instead turns every wobble of the learned rate into a phantom surplus that grows with the days since the last reset: in closed loops with variable weights or fractional rates, purchases then decayed to zero within two years (§13.3).
- **There is no expiry write-off** (measured: it cut precision from 51 % to 43 %).
- **Event order:** by arrival (delivery) date, then placement time, then order ID.
- **Applying an event:**
  1. If the event is user-initiated with q > 0, drop every normal batch that had **arrived before the event's order was placed**: its arrival time, i.e. its order's slot end or, when that is unknown, the end of its delivery date, is before the placement time. The user was running out of what was on his shelf when he decided to buy. A stock-up still on the shelf survives, and so does every batch still on its way, whether ordered before or after his decision.
  2. Add a stock-up batch of stockup_base and a normal batch with the rest.
- **History:** start empty at the first cached event, apply the history events in date order, then advance to e. An event outside the window whose base quantity is unknown is skipped (no batch, no reset); inside the window it makes the tag size-unknown (§6.2). Every projection starts there, so a stock-up bought before the window is still on the shelf (measured: starting at the window start let stock-up tags reach 13 weeks of stock).
- **Incoming stock** arrives at its delivery date; a date before e counts as e, and for an upcoming order the summary warns "delivery of <date> is overdue". It covers every placed, non-cancelled order outside history: upcoming orders, delivered orders with `delivery_date ≥ e`, and unexplained vanished orders (§6.1), each with its origin per §6.7.
- **The user-owned cart quantity** of g (§6.15) is a user-initiated event at T, placed now: it drops what is on the shelf now, but nothing still on its way. The new batch being planned arrives at T, after every known decision, so no known reset ever drops it.

### 6.10 Due, need, levels and amounts
Amounts are in base quantity; §6.13 turns them into units of the chosen SKU, or of several SKUs for a `variety` or `assorted` tag.
- **Horizon.** H = T + C days, with C fractional, so the order covers exactly one cycle, until the next delivery is expected. There is no safety margin and no rounding. Measured on the user's history: a 2-day margin for long-life items lowered precision at equal coverage, and rounding C up cost another point. Weighed pieces in a `kg` tag vary in weight, and the week ends within grams of empty, so the day before a delivery often runs a little short: accepted, since the user adds back what he went without and that order counts (§6.8). Loose produce eaten a piece at a time is counted in pieces instead (§10.4), where weight doesn't matter. A half-day margin for weighed tags was measured and rejected (§13.3).
- **Projected stock at T** = the stock on hand at T from the projection (§6.9), before the new batch. **Held** = projected stock at T + the base quantity of the incoming lines (§6.9) that arrive after T, so a top-up doesn't re-buy what is already on its way.
- **Due.** Project to T, then simulate [T, H) without a new batch: shortfall = r × (H − T) − consumed. The tag is **due** iff shortfall > ε.
- **Need** = the shortfall when due, else 0: the one-cycle part. It is exactly the smallest covering amount: the new batch arrives at T, after every known placement, so no reset drops it; every batch a reset can drop arrived earlier and is eaten first. A test checks it against a search over u.
- **Levels** of a long-life tag, over its window events with q > 0, every event whatever its origin (§1.1):
  - **usual batch** = Σ q ÷ the number of those events (`always`);
  - **MAX** = the largest q (`on_sale`).
  An `always` line roughly reproduces the usual batch, so the agent's own purchases barely move it: rounding to whole units and weighed products' actual weights shift it a little, and the user's cart changes move it for real, since the changed order counts. A top-up never takes the agent's order above MAX, so top-ups alone don't raise it. The need is never cut to MAX, though: when one cycle's need, rounded up to whole units, comes to more than MAX, the order covers it and becomes the new MAX. That is how rising consumption reaches MAX without the user; a weighed piece heavier than planned can also raise it slightly. MAX can drift down when every top-up in a year starts well above zero: the user then raises a top-up in the cart, and that order is MAX for the next 12 months. A tag with no window event has dropped out (§6.0, §6.11).
- **Amounts** by the tag's storage and mode (§6.11, §6.14). A line has a **normal** part and a **stock-up** part:
  - short-life, due: normal = need; no stock-up part;
  - `always`, due: normal = max(need, usual batch − the base quantity of incoming lines (§6.9) that arrive after H), bought on top of the projected stock; no stock-up part, whatever the prices;
  - `on_sale` with a qualifying sale and not waiting (§6.13, §6.14): normal = need (0 when not due); stock-up = MAX − held − the base quantity allocated to the normal part, when positive; both in the qualifying pool;
  - `on_sale`, due, with a qualifying sale while waiting: normal = need, in the qualifying pool; no stock-up part;
  - otherwise there is no line.
  Normal parts are normal stock; stock-up parts become stock-up batches (§6.7, §6.9).
- **Units.** The policy (§6.13) covers the normal part first, then the stock-up part, so each SKU s gets `normal_units` and `stockup_units`:
  - the normal part takes units until it reaches the need (round up). For `always`, units are then added while the next one brings the total nearer to its normal amount (a tie adds it);
  - the stock-up part takes units while held + this order's base quantity so far is below MAX (round up), but never a unit that would take this order's total base quantity above MAX: allocation stops at the first such unit. Rounding up keeps MAX alive when the user accepts every top-up (measured: rounding down decayed an `on_sale` tag to nothing within about two years, §13.3);
  - for a single SKU s of size z: normal_units = round up(need ÷ z), for `always` max(round up(need ÷ z), round(B ÷ z)) with B = usual batch − incoming after H; stockup_units = min(round up(stock-up ÷ z), round down(MAX ÷ z) − normal_units), at least 0;
  - weighed SKUs are ordered in pieces of their current size: their weight per piece in a `kg` tag, 1 (or their `units_per_pack`) in a `pcs` tag.
- **Caps**, applied in order:
  1. **Normal-purchase cap (short-life only):** the normal part is at most P90, the 90th percentile of q over the window's delivered events with q > 0. Units still round up, so at least one is bought. Long-life lines are bounded by the usual batch or MAX instead.
  2. **Cart cap**, applied while allocating: a SKU's final total (user-owned + agent units) ≤ min(`stock.maxAvailableAmount`, the cart line's `maxBasketAmount` when present). A SKU at its cap takes no more units: a spread policy moves on to its next candidate, a single one stops. The normal part is allocated first, so the cap cuts the stock-up part first. If not even 1 agent unit fits, the result is the suggestion `basket_limit`.
  - **Partial fill.** When a cap stops a line short, the line is added as capped, and the summary says "capped at your usual quantity" or "capped at the basket limit".
  - **Bounds.** A short-life need never exceeds one cycle; an `always` line is the user's usual batch, or one cycle when that is more; an `on_sale` top-up never takes the order above MAX, though the need alone, rounded up, can (see Levels).

### 6.11 Classification at e
The first matching rule wins. Every due tag that isn't added appears in the summary with its reason, except a locked one, whose line the user set himself (§6.15): no other due tag vanishes silently. Plan rows (§5.2) are stored for every rule that produces an output line (rules 4, 5, 10 and 11). All rows of a tag share one reason.

A **managed long-life tag** is a long-life tag with a stored stock-up mode (§6.14). It stays managed while it has at least one window event, however rarely it is bought.

1. In prefs `never` → excluded (shown only by `tags list`).
2. Paused (§6.12), or a `variety` tag whose mix types are all dropped → not proposed. Shown as a reminder (§2): in the first 3 normally finished non-dry runs that started after the order that paused it was placed, then only by `status`. An all-dropped `variety` tag reads "all types dropped — buy one to resume".
3. Locked in this cycle (§6.15) → not proposed, and its lines are left untouched.
   - For this run only, a tag in which an SKU the agent owns got no live data (§7.1 step 8) is left as it is too: being unable to check isn't the same as no longer wanting it. The summary says "couldn't check today — kept as it was".
4. Size-unknown (§6.2), with at least 2 events in the window, or a managed long-life tag → suggestion `size_unknown`, naming the product.
5. Due, n = 2, and not a managed long-life tag → suggestion `suggest_gate`: "bought twice in 6 months" ("in 12 months" for a long-life tag), naming the window so that it doesn't contradict a longer purchase count elsewhere.
6. No rate, or n < 3 (n = 0 for a managed long-life tag) → nothing. A long-life tag with a stored mode drops out on the date its latest event leaves the window (that event's date + 365 days) and gets a one-time notice (§2) dated then: "<tag> dropped out: not bought for 12 months — buy it once and it's back" (§8.1).

For the rest, the tag's storage and mode decide what it wants (§6.10, §6.14):
7. **Short-life:** due → a line of one cycle.
8. **`always`:** due → a line of the usual batch. Sales don't matter.
9. **`on_sale`:**
   - a qualifying sale (§6.13) while not waiting (§6.14) → a top-up to MAX: reason `due` when the tag is also due, else `stockup`. It wants nothing when it isn't due and the top-up comes to zero units (§6.10);
   - else, due with a qualifying sale while waiting → a line of one cycle (`due`);
   - else, due → suggestion `wait_sale`: "running low — waiting for a sale".
10. A tag that wants a line or `wait_sale`:
   - no candidate (§6.13) → `unavailable`, or `size_unknown` when no SKU has a known size;
   - else, for a line, the cart cap leaving no room for even one agent unit → `basket_limit`;
   - else, a top-up of a tag that isn't due, which the budget trims to zero (§6.14) → suggestion `budget_limit`: "on sale −40 % — skipped: stock-up budget reached";
   - else the line is **added**, or the suggestion `wait_sale` is shown.
11. A managed long-life tag that wants nothing, whose last purchase is more than twice its usual gap ago → suggestion `overdue`: "not bought for 9 weeks (usually every 4) — check your stock". It catches a tag whose estimate has drifted (§13.3). It is a reminder (§2), dated the day the gap was exceeded; its row is stored every run.
    - The **last purchase** is the latest placed, non-cancelled order containing the tag with q > 0, incoming orders included.
    - The **usual gap** = (b − a) ÷ (k − 1), where [a, b) is the rate's interval (§6.8) and k the number of distinct dates of the tag's events with q > 0 from a to b.
12. Otherwise nothing.

### 6.12 History replay: misses and pause
A **miss** is a line the agent added that the user removed before the order was placed. Process every placed, non-cancelled order in order of placement time, oldest first, over the whole history (nothing is pruned). For each order O_k:
1. **Added set A_k** — only for an O_k **matched** to a run (§6.6); for an unmatched (manual) order A_k is empty, because the user ordered without the skill. A_k is the tags, through the current mapping, that the agent added a line for in O_k's cycle:
   - the SKUs owned at close (§6.7) whose owner run's row has reason `due` or `stockup`;
   - the SKUs edited in O_k's cycle whose proposal run's row had reason `due` or `stockup` and whose proposal run owned them (`agent_owned[s]` > 0). A line the user changed just before a write that skipped it (§6.15) was never the agent's unless the agent already owned it.
   A line whose write failed never reached the cart, so it isn't in A_k. Suggestions (`suggest_gate`, `wait_sale`, `overdue`, `budget_limit`, …) are never added, so they never create a miss.
2. **Misses.** For each g ∈ A_k: if O_k contains a line of g (any SKU, any quantity, even if later claimed), there is no miss; otherwise the agent added g and the user removed it — a **miss**: misses[g] += 1. At misses[g] ≥ 2, g is **paused**.
3. **Clearing.** For every tag O_k contains (matched or not): misses := 0 and paused := false. Buying a tag, or asking the agent to add it, is how a paused tag comes back.
4. **Type misses** (`variety` tags only). For each matched O_k and each type s the agent offered (§6.7): if O_k has no units of s, type_misses[g, s] += 1; at 2, s is **dropped** from g's mix (§6.13). Any placed order containing s sets type_misses[g, s] := 0 and undrops s. A dropped type is never offered, so only the user brings it back. A tag whose types are all dropped is treated as paused (§6.11).

Misses count per placed order, never per day. A reduced quantity is not a miss, and neither is a swap between the SKUs of a `variety` or `assorted` tag. Removing a top-up counts like removing any other added line. Accepted noise: a line Rohlik fails to deliver looks like the user's removal.

### 6.13 Variant choice
Given an amount in base quantity (§6.10) — the normal part, then the stock-up part — the policy chooses which SKUs take the units, as one routine = **breadth** × **order**. The four presets: `cheapest` = single × cheapest; `solo` = single × preferred; `assorted` = spread × cheapest; `variety` = spread × proportional.
- **Candidates(g)** = SKUs of g that meet all of these:
  - bought at least once in the cached history, or the tag's preferred product (§5.3);
  - not ignored;
  - a known current size;
  - live data from this run;
  - orderable: `availabilityStatus` AVAILABLE with `maxAvailableAmount` > 0, or PREORDER with `expectedReplenishment` on or before T.
- **Fit** — the amount comes before the policy. A candidate whose single pack is more than 1.5 × what the line may take is dropped while another candidate fits; when none fits, only the smallest pack stays. What a line may take: P90 for a short-life line, max(need, usual batch) for `always`, max(need, MAX) for `on_sale` (§6.10). The policy then orders what is left, so `cheapest` spends least on what is actually needed: two 1 l packs at 30 CZK, not a 6 l pack at 25 CZK a litre.
- **Price** p(s) = (salePrice when its sale counts (§6.14), else originalPrice) ÷ current size. A SKU with no price is not a candidate.
- **Order** ranks the candidates:
  - **cheapest** — ascending p(s); ties go to the larger discount (the product that is usually pricier; a candidate's discount is Rohlik's marked discount when its sale counts, else 0, also when unknown), then, for `cheapest`, to the most recently bought and, for `assorted`, to the least recently bought (the oldest last placed order containing it, of any origin), then the lower SKU id;
  - **preferred** — the tag's preferred product (§5.3) first, when it is a candidate; the other candidates follow in the cheapest order;
  - **proportional** — by the recent mix (the `variety` block below).
- **Breadth** spends an amount within a **pool** P: all the candidates, or the qualifying pool (below):
  - **single** — all units on the first member of P in the order;
  - **spread** — one unit at a time to the member of P furthest below its share, with shares renormalised over P: equal shares counted in units for `assorted`, the recent mix for `variety`. Ties follow the order. With equal shares, n units reach n different members before any takes a second; with the mix, a type with a large share can take its second unit first.
  - A tag's normal part is allocated first and its stock-up part next, in the same pool (§6.10); the second phase counts the units the first one allocated. Units stop as §6.10 says. When the next unit doesn't fit, allocation stops; it never skips to a smaller pack.
  - The budget trims the stock-up part's last-allocated units first (§6.14).

- **`cheapest`** (single × cheapest): the whole amount on the cheapest candidate. When there was a choice, the summary notes "cheapest of N options".
- **`solo`** (single × preferred): the preferred product, whatever else the user buys now and then. If it isn't a candidate, or the fit rule dropped it, fall back to the first candidate in the cheapest order, noted "<preferred> unavailable — <pick> instead" (or "comes in a bigger pack than needed"); a fallback covers only the need, with no usual batch and no top-up. An `on_sale` `solo` tag has no fallback: without its preferred product, it is `unavailable`. Purchases never change the preferred product, and `solo` never switches for price: only the agent, for a new tag, or the user names one (§5.3). The fit rule applies to it like any candidate, since the amount comes before the policy. While g buys without its preferred product, or is `unavailable` for want of it, its plan rows record the preferred SKU (`fallback_from`), and the summary shows a reminder (§2) dated by the start of the streak's first run, i.e. the earliest stored run with such a row after the last plan row of g that used the preferred product: "eggs: Pohodová L unavailable (Vyprodáno.) — Milujeme M instead; name another to change it".
- **Preferred-product guess** (first run only, like the stock-up assessment of §6.14; stored when the run completes, §5.4). Eligible: each `cheapest` tag with `source` agent (a policy the user set is never guessed), without an `on_sale` mode, stored or assessed in this run, with n ≥ 3 (§2) and at least 2 SKUs with a purchase:
  - a SKU's **purchases** = the events counted in n in which its own line has a positive base quantity; its **share** = its purchases ÷ the sum of the purchases of the tag's SKUs. A line with an unknown base quantity doesn't count;
  - a SKU's **paid price** = Σ `line_price_czk` ÷ Σ gross base quantity (§6.14) over its window lines with a positive price and a positive gross base quantity; a SKU without such a line has none;
  - the **favourite** is the SKU with the largest share; ties go to the one bought most recently (its latest purchase, by delivery date, then placement time), then the lower SKU id. The tag becomes `solo` with the favourite as its preferred product, both with source `agent`, when its share is above 40 % and its paid price is above the mean paid price of the tag's other SKUs that have one. Otherwise it stays `cheapest`, also when the favourite has no paid price or no other SKU has one;
  - why the price: when the favourite is already the cheapest, `cheapest` buys it anyway, so `solo` matters only when it costs more. Measured against the user's own review (45 tags with 2 or more products bought): the rule guessed 13 tags, 7 of them his own `solo` choices, each with the product he named; the share alone guessed 25, 9 of them his, 2 with a product he didn't name;
  - why not `on_sale` tags: their paid prices are mostly sale prices, so "pricier" would measure the sales, and `solo` would limit their top-ups to one product's sales (on the user's data: mayonnaise, which he keeps `on_sale`);
  - a tag with one product bought is never guessed, nor is any tag after setup: from then on `solo` comes only from the agent or the user (§10.4). The setup report shows each guess with its evidence, e.g. "solo: prefers Pohodová vejce L — 9 of 15 purchases, 18 % pricier than the others you bought", and lists every guess again at its end, for the tag check (§8.2, §10.2).
- **`assorted`** (spread × cheapest): each unit goes to the candidate with the fewest units so far — its user-owned cart units plus this run's allocation — and among those to the first in the cheapest order. So n units go to the n cheapest different candidates, and none takes a second before every candidate has one. There is no cap per SKU: the user corrects the cart. The summary notes "the 3 cheapest of 13 options".
- **`variety`** (spread × proportional): the types are eaten from one shared slot, so the tag's pace limits their total (§6.8–§6.10), and the amount is split across the types in the household's recent mix.
  - **Mix types** = the SKUs of g bought in the window (any origin) that aren't dropped (§6.12). A type leaves the mix when it is dropped (§6.12) or when it has no purchase left in the tag's window; the latter gets a one-time notice (§2) dated the day its latest purchase leaves the window (§8.1).
  - **Mix:** w_s ∝ s's base quantity over g's last 6 choice events (§6.7), or its last 6 delivered events when it has no choice event, plus ½ × size(s). The floor keeps a type the user buys less often from vanishing.
  - **Allocated types** = the mix types that are candidates, with w renormalised over them. With no allocated type, the tag has no candidate (§6.11). A tag whose mix types are all dropped is treated as paused (§6.11).
  - **Allocation:** units are added one at a time to the type with the largest w_s × (Q + size(s)) − (Q_s + size(s)), with w renormalised over the pool, Q = Σ Q_s over the pool's types, and Q_s the sum of:
    - over g's last 6 placed, non-cancelled orders in which g was bought or offered (delivered or incoming): max(the order's base quantity of s, s's offered base quantity). A type offered and deleted has had its turn;
    - the base quantity of s's user-owned cart units;
    - what this run has already allocated to s.
  - Ties go to the least recently bought type, then the lower SKU id. Known limit: a type with a small share, sold in much bigger packs than the others, may rarely be offered, and then leaves the mix visibly.

- **Sale pool.** The **qualifying pool** holds the candidates whose sales qualify (§6.14), by policy:
  - `cheapest` and `assorted`: the candidates at the head of the cheapest order whose sales qualify, up to the first candidate whose sale doesn't. A sale thus counts only when the discounted product is also the cheapest per base unit: a pricier product on sale is never bought over a cheaper one at its regular price;
  - `solo`: the preferred product, when its sale qualifies;
  - `variety`: the allocated types whose sales qualify, with their mix shares.
  The tag **has a qualifying sale** iff its qualifying pool is non-empty. An `on_sale` tag allocates both its parts in the qualifying pool, so everything it buys is on sale; an `always` tag uses all its candidates.
- **Suggestion rows** (`wait_sale`, `basket_limit`, `budget_limit`, `overdue`, …) carry the first candidate in the policy's order, or for `variety` the most recently bought type (or, without a candidate, the most recently bought SKU), and show the tag's name.
- **No candidate:**
  - `unavailable`, with the `availabilityReason` of the most recently bought SKU, or "no live data"; for a `solo` tag, why its preferred product isn't a candidate: its `availabilityReason`, or what is missing ("pack size unknown", "no price", "no live data"), or "comes in a bigger pack than needed";
  - `size_unknown` when no SKU of g has a known size;
  - either way, no cart line.
- The chosen SKU, or a `variety`/`assorted` tag's allocation, is final for the run — amounts, caps and budget.

### 6.14 Discounts and stock-up modes
- **When a sale counts:** `salePrice` is set and the sale ends after today, i.e. `saleValidTill`'s date > e, or there is no end date. Top-up lines show the end date.
- **Discount** of s = Rohlik's marked discount, 1 − salePrice ÷ originalPrice. It is unknown when `originalPrice` is missing, and an unknown discount never qualifies. A sale **qualifies** iff it counts and its discount ≥ `stockup_min_discount` (0.35).
    - It is the only discount the skill uses.
    - Accepted risk: an inflated "was" price can overstate a discount. A wrongly triggered top-up is the user's to remove, and two removals pause the tag (§6.12).
- **Once per sale.** Tag g is **waiting** while a cycle closed by an order that isn't cancelled (§6.6) has a plan row of g with stockup_units > 0 and written stock-up (§6.7) > 0, whether the user then kept those units or not, whose `sale_until` is after e, or, for a row without a `sale_until`, whose closing order was placed less than 7 days before t. A waiting tag gets no top-up (§6.11 rule 9).
    - A top-up starts no wait until its order closes the cycle, so a re-run in the same cycle plans the same top-up again and writes nothing (§6.15). A top-up that failed to write, or that the budget trimmed to zero, starts no wait.
    - Another deep sale of the tag during the wait, on another SKU or with a new end date, adds no top-up: right after a top-up to MAX it would add little. A deep sale that runs for months keeps the tag waiting that long; its need is still bought on sale.
    - Deep sales almost always carry an end date: 83 of 91 recorded sales of 35 % or more did, with a median of 2 days left when seen.
- **Stock-up modes** (long-life tags only; short-life tags have none):
    - `always`: when due, the usual batch at whatever price (§6.10). Sales are a non-event: no top-up, no MAX, no budget. Prices still rank the candidates (§6.13);
    - `on_sale`: with a qualifying sale and not waiting, a top-up to MAX, due or not; when due with a qualifying sale while waiting, one cycle; when due without a qualifying sale → `wait_sale` ("running low — waiting for a sale"), and nothing is added.
- **Assessment** (first run only). The first run assesses each long-life tag with n ≥ 3 and no `stockup_mode` (§7.1 step 9), and stores the mode when it completes (it is a dry run, §5.4). Prices set a mode only then: the setup report shows each mode with its evidence, and the user confirms or corrects it in the tag check (§8.2, §10.2). Afterwards, a long-life tag that reaches n ≥ 3 without a mode gets `always`. A mode the agent or user set is never reassessed.
    - Take the tag's window events' lines with a positive `line_price_czk` and a positive gross base quantity — `weight_kg` for a weighed line in a `kg` tag, else quantity × size(s, the line's own `textual_amount`), before claims — of SKUs with a current `originalPrice` and current size.
    - A line was bought **on sale** when its paid price per base unit (`line_price_czk` ÷ gross base quantity) ≤ (1 − `stockup_min_discount`) × the SKU's regular price per base unit today (`originalPrice` ÷ current size).
    - If more than half of those lines' gross base quantity was bought on sale, the mode is `on_sale`, else `always`, also when no line is usable (e.g. product details failed).
    - The setup report shows each assessment with its evidence, e.g. "on_sale: 9 of 12 packs bought at least 35 % below today's regular price" (§8.2).
    - Accepted noise: a price that has risen since, or a free replacement pack merged into a paid line (§4.5), can make a full-price purchase look like a sale.
- **Eligibility for a top-up:** an `on_sale` tag that passes §6.11 rules 1–6, has a qualifying sale (§6.13) and isn't waiting; for `solo`, only on the preferred product.
- **Value** of a top-up: saved_czk = Σ stockup_units × (originalPrice − salePrice), and extra_spend_czk = Σ stockup_units × salePrice, over the line's SKUs.
- **Budget** (stock-up parts only; normal parts are never limited):
    - sort the top-ups by saved_czk, descending (ties: tag name, then `tag_id`);
    - accept while Σ extra_spend_czk ≤ `stockup_budget_czk` (1000);
    - trim the first that doesn't fit to the largest whole stock-up units that fit, then stop. A spread line trims its last-allocated stock-up units first. A top-up trimmed to zero keeps only its normal part; for a tag that isn't due, that leaves the suggestion `budget_limit` (§6.11).
- **Plan rows** keep `normal_units` and `stockup_units` separately, and a top-up's `sale_until`. All of a tag's rows share its reason: `stockup` for a top-up of a tag that isn't due, otherwise `due`.

### 6.15 Cart ownership, edits and writes
**Before planning:**
1. **Cycle.** Find the current cycle's runs and their last run L (§6.6). For this, a crashed `writing` run provisionally owns, per SKU: the `agent_owned` stored by a confirmed write or a skip (§5.4); else max(0, target − user_owned_before) for a SKU it meant to change; else before_qty − user_owned_before. With no run in the cycle, nothing is agent-owned.
2. **Recovery.** If the newest non-dry run is `writing`, it crashed. Finalise it now, in one transaction. Let x = the units of the order matched to it if one has closed its cycle, otherwise the current cart.
   - A SKU with a stored `after_qty`, from a confirmed write or a skip just before writing (§5.4), keeps the `after_qty` and `agent_owned` stored then. A confirmed write reached the cart and a skipped SKU kept its state, so any later difference is the user's edit, and edit detection (step 3) sees it: a removed top-up still counts as added (§6.12) and starts its wait (§6.14).
   - Another SKU the crashed run meant to change (target set and ≠ before_qty): after_qty := x[s], and agent_owned[s] := clamp(x[s] − user_owned_before[s], 0, max(target[s], before_qty[s]) − user_owned_before[s]). Recovery is exact only for confirmed writes and skips: an unrecorded outcome is inferred from the cart, so a line the user removed after such a write can come back once (accepted).
   - Any other SKU: after_qty := before_qty[s], and agent_owned[s] := before_qty[s] − user_owned_before[s]. The crashed run never touched it, so any difference from x is the user's edit, and edit detection (step 3) sees it.
   - state := `done`, recovered := 1; `finished_at` stays NULL.
3. **Edit detection**, skipped while a checkout is pending:
   - Each s with L.agent_owned[s] > 0 and cart[s] ≠ L.after_qty[s] was edited, by the user or by the agent at the user's request (§1.3).
   - It gets a `run_edits` row, and agent_owned[s] := 0. The row's proposal_run is s's **proposing run**: the latest run of the cycle, up to L, with a `due` or `stockup` row for s, or L when there is none. So a later edit of a locked tag's line still points at the run that proposed it (§6.7).
   - The **locks** are the edits detected by the current cycle's runs whose proposal run is also in the current cycle. A locked tag isn't proposed, and its lines are left untouched until the cycle closes.
4. **Ownership.** agent_owned[s] = L.agent_owned[s] for unedited s, and user_owned[s] = cart[s] − agent_owned[s] (at least 0).

**Targets:**
- a SKU with a `due` or `stockup` plan row: user_owned[s] + normal_units + stockup_units;
- a SKU agent-owned by L whose tag isn't locked or left as it is (§6.11 rule 3) and that has no such row: user_owned[s]. This removes, for example, an `on_sale` line after its sale has ended;
- no other SKU gets a target, so suggestions and unavailable rows never reach the cart.

**Write protocol:**
1. Commit the run as `writing` (§5.4).
2. Re-read the cart once. For each tag the run means to write (one with a SKU whose target ≠ before_qty): if any of its SKUs changed since the planning read, every SKU of the tag is skipped, so a shared need is never half-written. A skipped SKU gets after_qty := before_qty and agent_owned := before_qty − user_owned_before, its previous ownership. Each changed SKU gets a `run_edits` row, with its proposing run found as in step 3 but up to this run, or this run when there is none, so the tag is locked for the cycle, even when the line was absent before. These are stored in one transaction before the first write (§5.4). A change to any other tag is left to the next run's edit detection.
3. For each other SKU whose target ≠ cart[s]:
   - line present → `update_cart_item(s, target)` (target 0 only when user_owned[s] = 0);
   - line absent → `add_items_to_cart` with the target units; `update_cart_item` can't create a line (V4).
4. After a write error or timeout, never retry blindly:
   - re-read the cart, and continue if s already reached its target;
   - otherwise retry once: `update_cart_item(s, target)` when the line is present, `add_items_to_cart` with the missing units when it is absent, and `update_cart_item` when the re-read failed too, since it never adds on top of an unknown line;
   - if that fails too, report s. The retry's error decides: continue with the next SKU when it concerned s (e.g. sold out); stop writing on a transport or authentication error.
   A write is **confirmed** when Rohlik reports success or a re-read shows s at its target. Each confirmed write stores at once, in its own transaction (§5.4): after_qty[s] := target, agent_owned[s] := clamp(target − user_owned_before[s], 0, max(target[s], before_qty[s]) − user_owned_before[s]).
5. Re-read the cart. Every `run_cart` row without a stored after_qty (§5.4) gets one: before_qty when the run attempted no write to s, else the re-read cart, since a write that wasn't confirmed has an unknown outcome. Its agent_owned[s] = clamp(after_qty[s] − user_owned_before[s], 0, max(target[s], before_qty[s]) − user_owned_before[s]), with target = before_qty when NULL. Lines already at their target, locked lines and user-only lines keep before_qty, so a re-run that writes nothing keeps the agent's ownership. The re-read never replaces a known value: where the cart differs from after_qty, the user changed it during the run, and the next run's edit detection (step 3) sees it. These values are stored when the run is finalised as `done` at its end (§5.4, §7.1 step 13); every `run_cart` row of a `done` run then has non-NULL `after_qty` and `agent_owned`, which the engine asserts. A failure before that leaves the run `writing`, and step 2 of the next run recovers it.

A re-run with unchanged inputs makes zero writes. Residual risk (documented): an edit made in the app between the re-read and the write can still be overwritten, because the API has no conditional writes.

### 6.16 Minimum order and totals
- **After writing:** take the cart total from `get_cart` (`data.totalPrice`). If it is below `minimalStandardOrderPrice`, warn with the shortfall and Xtra's `orders_without_min_remaining`. The engine never adds items to reach the minimum.
- **Dry runs** report the observed cart total and, separately, the **projected** total.
  - projected = observed + Σ (target − cart) × unit price, over the SKUs with a target.
  - Unit prices come from product details, which the run reads for every cart SKU too (§7.1 step 8): `salePrice` when its sale counts, else `originalPrice` (not the per-kg `unitPrice`). A SKU without live details is left out, and the summary says the projected total is incomplete.
  - Planned removals and reductions therefore count, and the minimum-order check uses the projected total.

## 7. Runs

7.1 `run [--date YYYY-MM-DD] [--dry-run]` steps:
1. Acquire the lock; load `auth.json` and config.
2. No valid session → status `auth_required`.
3. Read `get_user_info().id`, then open and migrate `<account_id>/replenish.db`. If the ID differs from the last account in `auth.json`, add the note "different account than last time" and update `auth.json`.
4. Read the cart and upcoming orders, sync delivered orders, and resolve vanished orders (§6.1). If any of these reads fails, the run fails.
5. Fewer than 3 delivered orders in the last 183 days → status `insufficient_history`; the summary says how many exist and that 3 are needed. Stop.
6. Any SKU in history, upcoming orders or the cart that is still waiting for tags → status `needs_tags`. Stop. Product details are read for the listed SKUs first. Each listed SKU shows:
   - sku, name, latest text and weighted flag;
   - the parsed unit and size, or null. When the text doesn't parse, it shows a hint from price ÷ `unitPrice`;
   - times bought and last bought;
   - what it was bought together with in at least 2 orders, at most 8, most frequent first: product tags, and products still waiting for tags by name, so that the first run, when nothing is tagged yet, has co-purchases too;
   - its order-line texts when they differ from the latest one, e.g. "'2 l' in 3 orders, '500 ml' now": a pack that changed size, or a text that was wrong (§10.4). Texts that parse to the same size count as the same, and weighed products are never listed: their line text is that delivery's piece weight.
7. Resolve cycles (§6.6), recover a crashed `writing` run (§6.15), then find ownership at close, edits and user-owned quantities (§6.7, §6.15).
8. Read product details for the SKUs of every tag with n ≥ 2 or managed long-life (§6.11), and for every SKU in the cart, in batches of up to 20 IDs, with per-ID retry after a tool error (§4.4). If no requested ID returns details, the run fails here (stage `details`), before any write.
9. Run the history replay (§6.12), set stock-up modes (§6.14: the first run assesses them from prices; later runs give `always` to a long-life tag that reaches n ≥ 3 without one) and, on the first run, guess preferred products (§6.13), then build the plan (§6.4–§6.14).
10. Switch to a dry run when:
   - there is no `setup_completed_at`: this is the first run, and it gets the setup report (§8.2);
   - a checkout is pending or in progress (§6.6): the run gets its banner.
11. Unless dry: if there is no refresh token and the access token has under 60 s left → `auth_required`, with nothing written; otherwise write (§6.15).
12. Check the minimum order (§6.16) and read the checkout link; a failed link read is only a warning, which the summary shows.
13. Render the summary (§8.1) and finalise the run in one transaction (§5.4): the final cart state of §6.15, the assessed stock-up modes, the guessed preferred products and, for a first run, `setup_completed_at`. Release the lock.

A run that stops early — `insufficient_history`, `needs_tags`, `auth_required` at step 11, or any error before its write-ahead commit (§5.4), unexpected ones included — is stored as `failed`, together with every upcoming order it read, even when a later read of the same step failed. A run that fails after that commit stays `writing`, and the next run recovers it (§6.15).

7.2 A dry run writes nothing to the cart; this covers `--dry-run`, every first run, and a pending or in-progress checkout. It is stored with `state = 'dry'` and, apart from its upcoming-order observations and the stock-up modes and preferred products it set, ignored by §6.6–§6.15.

7.3 **Tagging handshake.** On `needs_tags`, the agent:
1. writes a JSON array `[{sku, product_tag, policy?, base_unit?, storage_tag, stockup_mode?, units_per_pack?, preferred?, ignore?}]`:
   - `storage_tag` is one of the seeded storage tags;
   - `policy` and `base_unit` are required for a new product tag. For an existing one, a value equal to the tag's is ignored and a different one is an error: change it with `tags set`;
   - `stockup_mode` (`always`/`on_sale`) is optional, for long-life tags: give it only when the user stated one. Otherwise the first run assesses it from the prices the user paid and later runs default it to `always` (§6.14); the user can change it. For an existing tag it follows the rule for `policy`, and it is rejected for a short-life tag (§5.3);
   - `units_per_pack` is required where the SKU's text doesn't parse in the tag's base unit, because the engine rejects the entry otherwise. Give a number in the tag's base unit (e.g. from "cca 500 g" in the name), or an explicit null if it can't be determined;
   - `preferred: true` marks the product a `solo` tag buys (§5.3). A new `solo` tag needs exactly one such entry, and an entry with `ignore` can't have it. On an existing tag only `--source user` may give it, to name a new preferred product; with `--source agent` it is rejected there. Give it per §10.4;
2. runs `tags apply <file>`;
3. fixes any validation errors;
4. re-runs `run` with the same options.

The engine maps names to `tag_id`s and creates new product tags as needed.

7.4 **Expected runtime:** the first run takes up to ~3 minutes (a 12-month history sync and ~300 product lookups); later runs take under a minute. The very first command on a machine also waits for uv to provision Python and the locked packages. `SKILL.md` tells the agent to allow for this.

## 8. Output

8.1 **Summary.** Markdown rendered by the engine in English, with product names as in Rohlik (followed by the pack text in brackets where two products of a tag share a name), and presented verbatim. It is short by design: in a writing run the cart itself shows what was added, and `data` (§8.3) keeps every plan line with its reasons, so the agent can answer "why X?" on request.

**Header:** one line with T and its source ("your date", "earliest slot" or "tomorrow, no slot found") and the assumed next delivery, T + C; then banners for: dry run; checkout pending; checkout in progress; different account; vanished orders; overdue deliveries.

**Sections**, in this order, omitting empty ones:
1. **Would add** (dry runs only, since the cart doesn't show them): product · quantity, one line each, top-ups marked.
2. **Topped up on sale** (`on_sale` tags): one line per tag: product · top-up quantity · Rohlik's discount · "until <date>". Without it, a bigger-than-usual line would look like a mistake; removing it counts as removing a line (§6.12) and starts the sale's wait (§6.14).
3. **Suggestions (not added):** product · reason: "bought twice in 6 months", "running low — waiting for a sale", "not bought for 9 weeks (usually every 4) — check your stock" (a reminder, §2), "on sale −40 % — skipped: stock-up budget reached", "pack size unknown: <product>", "size in another unit: <text>", "basket limit reached", "unavailable: <Rohlik's availability text>" for a tag that wanted a line but has no candidate, and "couldn't check today — kept as it was" (§6.11 rule 3).
4. **Reminders** (§2): a paused tag, "Paused: milk — removed twice; buy it once, or ask me to add it, and I'll resume"; a type dropped from a `variety` mix: "Gouda 48+ dropped from sliced cheese — removed twice; buy it once and it's back". Also the one-time notices (§2): a type that left a `variety` mix ("Goat cheese slices left the sliced-cheese mix: not bought for 6 months"), and a long-life tag with a stored mode that dropped out ("toilet paper dropped out: not bought for 12 months — buy it once and it's back"). Also as a reminder, a `solo` tag that couldn't buy its preferred product (§6.13): "eggs: Pohodová L unavailable (Vyprodáno.) — Milujeme M instead; name another to change it".
5. **Check these tags** (only when the agent tagged products since the previous run, or the 1 → 2 migration set preferred products: then it lists every `solo` tag with its preferred product, except those the user named himself, in every run until a non-dry run finishes normally, which clears `preferred_check`). The user's own corrections are never listed back to him: grouped by storage tag, each tag with its policy (for `solo`, its preferred product), its stock-up mode if long-life, "counted in pieces" or "by weight" if it has weighed products, and its new products, e.g. "Chilled — yogurt fruit (variety): Hollandia blueberry, Hollandia raspberry"; also possible wrong merges, i.e. SKUs of one tag bought together in at least 2 orders (except `variety` and `assorted` tags, whose SKUs are bought together by design). It ends: "Tell me if any is wrong; otherwise they stay." (§10.2 step 5).
6. **Warnings:** the minimum-order shortfall and the Xtra exemptions left; skipped or failed writes; "planned for delivery on T — re-run with `--date` if you pick another day", and without a free slot §6.5's "no free delivery slot in the next 4 days" or "couldn't read the delivery slots for N of the next 4 days"; a checkout link that couldn't be read.
7. **Totals:** the cart total from Rohlik (for dry runs, the observed and the projected total); top-up spend and savings.
8. **Checkout link.**

8.2 **Setup report** (first run). The engine writes the full report to `<account_id>/setup-report.md` in the data directory and returns its path as `data.report_path`; `summary_md` carries a compact version that links to it, since the full report runs to several hundred lines. The full report, shown before the plan:
- **What tags are**, in plain words:
  - a tag is a need, not a product, and products in one tag are interchangeable for the household;
  - usage and purchases are counted per tag;
  - the skill picks products within a tag by its policy: `cheapest` always takes the cheapest available one; `solo` takes your preferred product, whatever else you buy now and then, and the cheapest available one only when it is unavailable — guessed where one product makes up over 40 % of a tag's purchases and costs more than the others you bought; `variety` mixes the types you eat from one shared slot, such as sliced cheeses, in your recent proportions; `assorted` takes one each of the several cheapest, for things you buy a few kinds of at once, such as pasta shapes;
  - "same use" matters: a 1 kg curd bucket for baking and 250 g tubs for eating are different tags;
  - a wrong grouping causes wrong substitutions and quantities;
  - a tag that is too narrow may never reach 3 purchases, and is then never added.
- **Stock-up.** Short-life tags are bought one delivery at a time. Long-life tags (every product keeps) have a stock-up mode, guessed from the prices you paid over the last 12 months:
  - `always`: when it runs low, your usual batch (your average purchase) is added, whatever the price;
  - `on_sale`: on a sale of at least 35 %, it is topped up to your largest purchase, once per sale; when it runs low without one, you only get a reminder.
  Each guessed mode is shown with its evidence in the mapping below; tags that qualify later default to `always`. Every order counts, whoever filled the cart: when a quantity is wrong, change it in the cart, and the skill learns from that order.
- **Full mapping, grouped by storage tag:**
  - within each storage tag (with its stock-up flag), the product tags by how often they are bought, each with its policy (for `solo`, its preferred product, and for a guessed one its evidence, §6.13), its stock-up mode and evidence if long-life, "counted in pieces" or "by weight" if it has weighed products, and its products (name, pack size, times bought);
  - highlighted: multi-product tags, `solo`, `variety` and `assorted` tags, possible wrong merges, unknown pack sizes, order-line texts that differ from today's (§7.1 step 6), and ignored SKUs.
- **How to correct**, by telling the agent:
  - split, merge or rename a product tag;
  - move a product;
  - change a policy, a `solo` tag's preferred product, a stock-up mode, or a product's storage tag;
  - count a tag's weighed products in pieces or by weight;
  - group types eaten from one shared slot into a `variety` tag, or kinds bought several at a time where only the price matters into an `assorted` tag;
  - edit a storage tag's stock-up flag;
  - ignore a product;
  - mark a product tag "never".
- **History:** depth and cadence; how many tags will be added when due, suggested or ignored.
- **Backtest** on the user's own history (§13.4), with a one-line interpretation, e.g. "about half of what I'd add you bought that week; about 7 in 10 within two orders". Precision reads "n/a" when nothing was proposed.
- **The cart this run would have built**, in §8.1's sections ("Would add" first), marked "nothing was written".
- **Check these tags:** the report ends with every guess in one short list, each with its evidence in a few words: the `solo` guesses with their preferred products (§6.13) and the `on_sale` modes (§6.14). It asks the user, once, to confirm or correct them and the tags, policies and modes above; anything he doesn't correct stays (§10.2 step 5).
- **The compact version** (`summary_md`): the header and banners; in full, the tags that can be added or suggested (n ≥ 2, or a managed long-life tag) and every flagged tag — multi-product, `solo`, `variety`, `assorted`, a guessed mode or preferred product, a possible wrong merge, an unknown pack size or differing order-line texts, an ignored product; for the other tags, a count per storage tag, e.g. "Chilled: 41 more tags, each bought once"; the history and backtest lines; the cart this run would have built; "Full report: <path>"; and the guess list and the tag check, which end it.

8.3 **Envelope.** Every command prints exactly one JSON envelope to stdout, in UTF-8, also when it fails unexpectedly (status `error`, stage `internal`):
```json
{"status": "ok|dry_run|needs_tags|auth_required|insufficient_history|error",
 "summary_md": "…", "data": {}, "warnings": []}
```
- `data` for `run`: T, C, plan lines, writes and totals; on the first run, also `report_path` (§8.2).
- `data` for `needs_tags`: the SKU list.
- `data` for `error`: `{stage, wrote, actions}`; `summary_md` says the same in words.
  - `stage`: the step that failed. For `run`, it is named after its §7.1 step (e.g. `lock`, `sync`, `details`, `write`); for other commands, it is the command;
  - `wrote`: whether a cart write was attempted before the failure. A run that failed after its write-ahead commit is recovered by the next run (§5.4, §6.15);
  - `actions`: at least one suggested action in plain words, e.g. "another run is in progress: wait for it to finish" or "Rohlik didn't answer: try again in a few minutes".
- `login` also prints its URL or code to stderr (§4.2).

## 9. CLI

`uv run replenish.py <command> [options]`. Test-only options:
- `--fixtures DIR`: recorded responses replace the MCP client;
- `--now ISO`: only with `--fixtures`, or in `backtest`.

| Command | Purpose |
| --- | --- |
| `login` | Interactive login (§4.2). |
| `run [--date YYYY-MM-DD] [--dry-run]` | One run (§7). |
| `tags list [--tag TAG]` | Product tags with their SKUs, storage tags, stock-up modes and, for `solo` tags, preferred products, plus the storage tags' attributes, as Markdown. |
| `tags apply FILE [--source agent\|user]` | Assign SKUs (and sizes) to tags in one transaction, creating new product tags. The agent uses `--source user` for the user's corrections. |
| `tags rename TAG NEW_NAME` | Rename a product tag; its `tag_id`, preferences and history stay. |
| `tags merge FROM INTO` | Move FROM's SKUs and preferences into INTO, then delete FROM; INTO keeps its policy, preferred product, base unit and stock-up mode, taking FROM's mode when it has none (§5.3). |
| `tags set TAG [--policy cheapest\|assorted\|solo\|variety] [--preferred SKU] [--stockup-mode always\|on_sale] [--note TEXT] [--base-unit kg\|l\|pcs\|m]` | Change a product tag's attributes; a base-unit change is validated against its SKUs, and `--stockup-mode` is rejected for a short-life tag. `--preferred` names one of the tag's SKUs and makes the tag `solo`; `--policy solo` needs a preferred product (§5.3). |
| `tags storage-set NAME --stockup yes\|no` | Set a storage tag's stock-up flag (the set itself is fixed). |
| `prefs never add\|remove TAG` | Maintain the never list. |
| `prefs cadence DAYS\|auto` | Override or reset the cadence. |
| `backtest [--csv DIR] [--raw-units]` | §13.4. |
| `status` | History depth, cadence, last run, owned lines, locks, paused tags, dropped types, and live precision and recall over the last 5 matched orders (agent lines kept; ordered tags that had been added). |

- TAG arguments accept a tag name or a `tag_id`.
- Every `tags` command that writes takes `--source agent|user` (default `agent`); `user` assignments are never overwritten by the agent (§5.3).
- Exit codes: 0 for `ok`/`dry_run`; 2 for `needs_tags`, `auth_required` and `insufficient_history`; 1 for `error`.
- Command summaries stay short: counts, naming at most 10 items (e.g. `tags apply` says how many tags it created and names the first 10). `tags list` flags a product whose order-line texts differ from today's (§7.1 step 6).

## 10. SKILL.md and tagging rules

10.1 **Frontmatter:** `name: rohlik-replenish`. The `description` triggers on:
- filling, preparing or restocking the Rohlik cart, and reordering the usual groceries;
- tag corrections, e.g. "milk 1.5 % and 3.5 % are different";
- standing preferences, e.g. "never order X", "only buy X on sale" or "P is my preferred X".

10.2 **Procedure for the agent.** Steps 2–4 are stages: each starts only after the previous one has succeeded. When a stage fails, the agent stops, tells the user which stage failed and why, and suggests what to do. It carries out an action only when the user chooses it, and never works around the failure, e.g. by editing the cart or the data files.
1. **The user has precedence.**
   - Explicit instructions override this skill's rules.
   - One-off requests ("I'm out of X", "skip Y this time", "add Z") are outside the engine. Carry them out yourself after the run, for example by editing the cart with Rohlik's tools; the engine treats those changes as the user's own.
   - Otherwise the skill fills the cart autonomously: run it, and go.
2. **Dependencies.** Check `uv --version`.
   - uv missing, or older than the minimum version `SKILL.md` states (the oldest that reads the committed lockfile) → report it and suggest installing or updating it user-locally, without administrator rights, with Astral's official installer, giving the exact command for the OS. Once the user agrees, run it and continue, calling uv by its full path if the current shell doesn't see it yet.
   - Python and the locked packages need no stage of their own: `uv run` provisions them on first use (§7.4).
3. **Run** `run`, adding `--date` when the user names a delivery day and `--dry-run` when they only want to see what would be ordered. Then handle the status:
   - `auth_required` → run `login` so that its output reaches the user while it waits, tell them to finish logging in in the browser (routine), then re-run;
   - `needs_tags` → tag per §10.4, run `tags apply`, re-run. It can come on any run, whenever a new product appears in an order or the cart;
   - `insufficient_history` → present the summary and stop;
   - failures:
     - `error` → report the failed stage, whether the cart was changed, and the suggested actions (§8.3); don't improvise a workaround;
     - no JSON envelope on stdout → uv failed before the engine started, e.g. while provisioning without a network: report uv's error output and suggest actions.
4. **Present** `summary_md` verbatim, with no recomputation, reordering or omission, and at most two sentences of commentary. It is short by design (§8.1); answer "why X?" from `data`. When `data.report_path` is set (the first run), also give the user that file as a link. After a writing run, ask the user, in one line, to check each item's count in the cart before checking out: a changed count is how the skill learns their amounts (§1.1). After a dry run the cart is unchanged, so skip that line.
5. **Tag check.** When the summary ends with "Check these tags" (the first run's setup report, or products tagged since the previous run), that is the run's one question: apply each correction the user gives with `tags …` and `--source user`; if he doesn't answer, the tags stay as they are. The first run is always a dry setup run (§7.1 step 10): once the user has answered its tag check, or tells you to go ahead, run again with the same options; that run fills the cart.
6. **Corrections:**
   - tag corrections → `tags …` with `--source user`;
   - pack-size suggestions ("pack size unknown", "size in another unit") → `tags apply` with `units_per_pack`;
   - a standing preference — "P is my preferred X", "always buy P for X", "from now on P", "make P solo" → `tags set X --preferred P --source user`, which makes X `solo`; for a `variety` or `assorted` X, say that its mix ends. When P is in another tag, move it into X first with `tags apply --source user`. When P has never been in an order or the cart, say that the choice can be made once it has, and offer to add it to this cart. A one-off ("buy P this time") is a cart edit (step 1), not a preference;
   - "never order X" → `prefs never add`;
   - "only buy X on sale" or "always buy X" → `tags set X --stockup-mode on_sale|always --source user` for a long-life tag. For a short-life tag, explain that it is bought one delivery at a time and can't wait for a sale.
   Show the result with `tags list --tag`.

10.3 **Authorization.** Invoking a non-dry `run` is the user's authorization to change the cart as planned.
- The agent MUST NOT:
  - check out or reserve timeslots;
  - touch the database or other data files;
  - compute quantities itself;
  - ask the user which products, quantities or variants to put in the cart (zero-touch: the plan is the engine's). Allowed: reporting a failed stage and waiting for the user's choice (§10.2); the count-check reminder of §10.2 step 4, which asks the user to review the cart; and the tag check of step 5, which asks how products are grouped, not what to buy;
  - change the cart on its own initiative.
- Outside a run, it changes the cart only when the user explicitly asks (§1.3).
- It allows for the runtimes of §7.4.

10.4 `reference/tagging.md` MUST state:
- **A tag is a need.** Two products share a tag only if the household would accept either for the **same use**. **Merge what substitutes; split what is bought together:** five apple varieties in one order are five tags. The exceptions, made only at the user's request: a `variety` tag, for types the household eats from one shared slot, such as sliced cheeses for sandwiches; and an `assorted` tag, for kinds bought several at a time where only the price matters, such as pasta shapes.
- **Same use matters.** These are separate tags: a 1 kg curd bucket (baking) and 250 g tubs (eating); 500 g breakfast muesli and 50 g snack bars; lasagne sheets and other pasta. A pork-and-beef mince mix and beef mince are one tag if they are used the same way.
- **Names:**
  - short lowercase English that includes the attributes that matter: type, fat %, flavour, cut, size class, decaf, lactose-free, and organic when it matters;
  - examples: "milk 1.5%", "milk 3.5%", "eggs size l", "minced meat", "greek yogurt plain 5%";
  - **never a brand**, and never a pack size.
- **When unsure,** keep products in separate tags.
- **Policy:** `cheapest` by default; `solo` when the specific variant matters to the household, e.g. "organic fresh milk regardless of price", coffee, or baby food, marking its preferred product with `preferred: true` when the tag is new: the one the user named, else the one bought most. On an existing tag the preferred product changes only when the user names one (§5.3); `variety` and `assorted` only when the user asks for them. The first run also guesses `solo` from purchases, for the user to confirm in the tag check (§6.13). A claim that must never be lost (organic, lactose-free, gluten-free) belongs in the tag name.
- **Stock-up mode** (long-life tags): the first run guesses `always` or `on_sale` from the prices the user paid, for the user to confirm in the tag check; later tags default to `always`. The agent sets one only when the user states it. Short-life tags have none.
- **Base unit:** `kg`, `l` or `pcs`; `m` for products Rohlik prices per metre (toilet paper, kitchen towels), so that prices compare per metre.
  - **Weighed products** (⚖: sold by the piece, charged by weight) go in a `pcs` tag when the household eats or uses each piece whole, one at a time: loose fruit, peppers and cucumbers, such as bananas, apples, citrus, plums and melons. A lighter piece then doesn't make the plan short. A weighed bunch or pack of such pieces in the same tag gives its piece count as `units_per_pack` (a ~0.7 kg bunch of bananas: 4).
  - Every other weighed product goes in a `kg` tag: vegetables cooked by weight (potatoes, carrots, onions, cabbage, squash), meat, fish, deli and cheese, and any tag that mixes loose pieces with bags or packs.
- **Storage tag** (per SKU) = how that product is stored:
  - fresh dairy is `chilled`, UHT milk is `dry`, and frozen products are `frozen`;
  - fresh and frozen variants may share a product tag when the household uses them the same way.
- **Pack sizes** are parsed by the engine from the text. The grammar:
  - a number with `,` or `.` decimals, followed by `g`, `kg`, `ml`, `l`, `ks` or `m`, and nothing else: `80 mytí` (80 washes) doesn't parse;
  - `N x size` and `N× size` multiply;
  - prefixes such as `cca` are ignored.
  - A weighed product's size is its weight per piece in a `kg` tag, and 1 piece in a `pcs` tag.
  - The agent resolves only what the engine reports as null or "in another unit", from the name or text, in the tag's base unit — or leaves it null.
  - Order-line texts that differ from today's (§7.1 step 6) are shown, not resolved: give `units_per_pack` only when the name or the user says which is right (e.g. "2×250 ml" in the name against an old "2 l"), since packs do change size.
- **Non-products** (catalogues, deposits, gifts) → `ignore: true`.
- **Never change** `source: user` assignments.

## 11. Configuration

`config.toml` (optional) holds only the user's preferences:

| Key | Default | Used in |
| --- | --- | --- |
| `stockup_min_discount` | 0.35 | §6.14 |
| `stockup_budget_czk` | 1000 | §6.14 |

Everything else is a constant of the engine; the backtest may vary constants through its own options:
- **History:** window 365 days for long-life tags and 183 for short-life (§6.0); first-sync depth 365 days; minimum history 3 delivered orders in the last 183 days.
- **Gates:** 3 purchases to add, 2 to suggest when due; a managed long-life tag needs 1 (§6.11). Backup reminder after twice the usual gap without a purchase.
- **Cadence:** last 8 gaps, clamped to [4, 21] days; 7 with fewer than 2 gaps.
- **Quantities:** short-life need capped at the P90 purchase; `always` adds the usual batch (average purchase), rounded to the nearest unit; `on_sale` tops up to MAX (largest purchase) once per sale, the top-up rounding up but never taking the order above MAX; after a top-up the tag waits until that sale ends, 7 days for an undated one; a `solo` guess (first run only; `cheapest` tags the agent set, without an `on_sale` mode) needs n ≥ 3, at least 2 products, and one with over 40 % of the purchases and a higher paid price than the others (§6.13); a `variety` mix uses the last 6 choice events plus a floor of ½ unit per type, and its rotation the last 6 placed orders.
- **Learning:**
  - 2 misses (a line the agent added and the user removed) to pause a tag; 2 type misses to drop a type from a `variety` mix;
  - paused tags, dropped `variety` types and `overdue` suggestions shown as reminders in 3 normally finished non-dry runs (§2).
- **Calls:** product-details batches of 20.

**Storage tag seeds**, inserted by the schema migration; the flag is editable with `tags storage-set`:

| Storage tag | `stockup_eligible` |
| --- | --- |
| bakery | 0 |
| fresh-produce | 0 |
| chilled | 0 |
| fresh-meat-fish | 0 |
| frozen | 1 |
| dry | 1 |
| drinks | 1 |
| drugstore-household | 1 |
| pet | 1 |
| other | 0 |

## 12. Safety and robustness

- **Allowlist.** The tool allowlist (§4.3) is enforced inside the MCP client wrapper and unit-tested.
- **Fail closed:**
  - a failed upcoming-order or cart read fails the run;
  - an unexplained vanished order stays incoming stock;
  - a SKU without live price or availability gets no line, and a tag whose agent-owned SKU got no live data keeps its lines as they are;
  - a tag with an unknown pack size is never added.
- **The engine never:**
  - clears the cart;
  - lowers a user-owned quantity;
  - writes in a dry run or a first run (tested).
- **Writes.** Writes are never retried automatically. The run is committed as `writing` before its first write, each confirmed write is stored at once, and the run is finalised at its end; a crashed run is recovered by the next one, and re-runs are idempotent (§6.15).
- **Failure reports.** Every `error` names its stage, says whether the cart was changed and suggests actions (§8.3). The agent acts on them only with the user's consent (§10.2).
- **One command at a time,** enforced by an OS lock that the operating system releases if the process dies.
- **Upgrades.** A migration is one-way and an older engine refuses a newer schema (§5.1), so README tells the user to copy `replenish.db` before the first run of a build with a newer schema, and to make that run a dry run when the migration fills in choices for him to check (§8.1). The engine never restores a copy.
- **Privacy.** No raw responses or profile fields are stored or logged, and secrets live only in the keyring.
- **Determinism.** `now` is injectable, arithmetic is in whole days, every planning query has an explicit ordering, and output is UTF-8.

## 13. Testing and acceptance

13.1 **Unit tests:** at least one per rule, including its boundaries.
- **Input boundary:**
  - sizes: live text vs line text vs override; a weighed line's own piece weight; another unit → unknown with a report; a weighed SKU in a `pcs` tag is 1 per piece, or its `units_per_pack`, whatever it weighs;
  - line base quantities: weight, claims and unknowns; a line delivered today uses its actual weight and claims; a weighed line in a `pcs` tag counts its pieces minus claimed pieces and ignores its weight; switching a weighed tag between `kg` and `pcs` re-counts its history in the new unit;
  - a stored size or override whose unit differs from the tag's current base unit counts as unknown;
  - the warranty piece-count fallback;
  - the `textual_amount` grammar: `2 x 100 g` = 0.2 kg, `6×1,5l` = 9 l, `cca 190 g`, `308,8 m` = 308.8 m; `80 mytí` doesn't parse;
  - events with q = 0: no reset.
- **Cadence:** median, clamp, override, fewer than 2 gaps; a fractional C gives a fractional horizon.
- **Rate:**
  - the interval runs from the first to the last window event date, every event counting whatever its origin; the event at b isn't counted; the whole-history fallback takes the two most recent distinct dates;
  - no interval → no rate;
  - an accepted agent line doesn't reset the projection, and a user addition does.
- **Stock transitions:**
  - FIFO, with same-day normal batches before stock-up batches;
  - each past day consumes the rate known that day: a history whose rate steps from 1 to 2 units a week replays each stretch at its own rate, and a replay at today's rate gives a different stock; days before the first rate use the first rate; a closed loop under A with fractional rates and noisy consumption doesn't decay: every tag is bought at ≥ 80 % of consumption in every block (§13.3). Its shortage days aren't asserted, since ±30 % daily noise with no margin always causes some;
  - a reset drops only normal batches that had arrived before the resetting order was placed. Test case: O1 = 4 units placed 6 Oct, arriving 8 Oct; O2 = 1 unit, user-initiated, placed 7 Oct, arriving 9 Oct. At T = 9 Oct, 4 units are on hand, not 1;
  - same-day events apply in placement order, and a same-day reset compares the slot end with the placement time, an unknown slot counting as the end of the day;
  - projection starts at the first cached event, so a stock-up bought before the window still counts;
  - no write-off;
  - an event outside the window with an unknown base quantity is skipped.
- **Incoming stock:** an order delivered today; an overdue upcoming order; a vanished order that is kept, then dropped after its delivery date; a cancelled order that is dropped.
- **Due and need:**
  - the closed form equals a search over u in randomised cases, including empty stock, a user order already placed for day 3, and the planned batch surviving that order's reset;
  - a user cart line as a reset.
- **Caps:**
  - the short-life P90 cap over positive quantities, still at least one unit; long-life lines aren't P90-capped;
  - the cart cap with a user-owned quantity; applied while allocating, so a spread tag moves to its next candidate and the stock-up part is cut first;
  - a capped line is added and reported as capped.
- **Classification:** precedence; the reason codes, one per tag; an `on_sale` top-up is `due` when also due, else `stockup`; `suggest_gate` only when due; a tag that wants a line or `wait_sale` but has no candidate is `unavailable`; a not-due top-up the budget trims to zero is `budget_limit`; a managed long-life tag is gated at n ≥ 1 (with one window event its details are read, an `always` tag is added when due, an `on_sale` tag is topped up on a deep sale, and an unknown size gives `size_unknown`), never gets `suggest_gate`, gets `overdue` after twice its usual gap without a purchase, and gets a one-time notice when it drops out.
- **One-time notices:** a notice dated d shows in every run that starts on or after d, dry runs included, up to and including the first non-dry run that starts on or after d and finishes normally; a crashed run, even once recovered, doesn't end it; nothing dated on or before the day setup completed is shown, so a first run lists no old drop-outs; a tag that drops out, is bought, and drops out again gets a second notice.
- **Reminders:** a paused tag, a dropped type and an `overdue` suggestion show in the first 3 normally finished non-dry runs that started after their moment (§2), then only in `status`; a run earlier on the day of the pausing order doesn't count, and neither does a recovered crash.
- **Fit:** a 6 l pack at 25 CZK/l against a P90 of 2 l loses to 1 l packs at 30 CZK; a pack within 1.5 × of the line's limit stays a candidate; when none fits, the smallest pack; `solo` falls back when the fit rule drops its preferred product.
- **Missing live data:** a tag whose agent-owned SKU got no details keeps its lines and isn't re-planned; details failing for every ID fail the run before any write; a batch failing on transport isn't split into single IDs; the read budget stops a hung run.
- **Cart reads:** a line without a readable quantity, or no `items` map, fails the read, with zero writes.
- **Auth:** an expired access token with a valid refresh token refreshes silently in a fresh process; a rejected refresh gives `auth_required` without opening a browser; a keyring failure gives an `error` envelope; without a refresh token, the lifetime check uses the time at the start of the write phase.
- **Envelopes:** every command prints exactly one envelope, also on an unexpected exception (stage `internal`); a run failing before its write-ahead commit is stored as `failed`, with the upcoming orders it read.
- **Miss rule:**
  - a line the agent added and the user removed is a miss; 2 misses pause; a purchase resets and unpauses;
  - a removed top-up is a miss like any other line;
  - a reduced quantity is not a miss, and neither is a swap between the SKUs of a `variety`/`assorted` tag;
  - a `due`/`stockup` line whose write failed was never added, so it isn't a miss;
  - a SKU the user changed just before a write that skipped it, which the agent didn't own, is never a miss;
  - an unmatched (manual) order creates no miss, but still clears;
  - a suggestion (`suggest_gate`, `wait_sale`, `overdue`, `budget_limit`, …) is never added, so it never misses;
  - `variety`: a type the agent offered and the user removed twice is dropped; one removed once then bought isn't; buying a dropped type brings it back; a tag whose types are all dropped is treated as paused.
- **Stock-up modes and amounts:**
  - short-life tags have no mode; a tag with any non-eligible SKU is short-life;
  - `always`: due → max(need, usual batch − incoming after H) on top of what is left, rounded to the nearest unit and never below the need; a qualifying sale changes nothing; not due → nothing;
  - `on_sale`: a qualifying sale tops up to MAX whether or not it is due, against held stock including incoming orders after T; the stock-up part rounds up but never takes the order above MAX, while a need above MAX, or rounded up above it, is covered in full; every unit is on a qualifying sale, both parts from the qualifying pool; due while waiting → one cycle; due without a qualifying sale → `wait_sale`;
  - once per sale: a re-run in the same cycle plans the same top-up and writes nothing; after the order closes the cycle, the tag waits until that top-up's sale ends, whether the user kept or removed it, and another deep sale during the wait (another SKU or end date) adds no top-up; from the day the sale ends, a new qualifying sale tops up again; a failed or budget-zeroed top-up starts no wait; an undated top-up waits 7 days after its order;
  - the usual batch and MAX count every window event, the agent's included; an `always` line roughly reproduces the usual batch; a top-up never takes the agent's order above MAX; units the user adds to a top-up line count as stock-up;
  - assessment, first run only: paid vs today's regular price per base unit, by gross base quantity before claims; a line with zero gross quantity is skipped; more than half at least 35 % below → `on_sale`, else `always`, also with no usable line; stored by the first run (a dry run); after setup, a long-life tag reaching n ≥ 3 without a mode gets `always`, never `on_sale`; a mode set by the agent or user is never reassessed;
  - the 12-month window keeps a rarely-bought long-life tag addable, and a long-life gate/pace uses it.
- **Variants:**
  - `cheapest`: the lowest price per base unit; ties to the larger discount (an unknown discount counts as 0), then the most recent;
  - `assorted`: n units go to the n cheapest different candidates, and a second unit only once every candidate has one; a user-owned cart unit counts toward its SKU; ties to the larger discount, then the least recently bought; no cap per SKU;
  - sale pool: for `cheapest`/`assorted`, the qualifying candidates at the head of the cheapest order — a pricier product at 40 % off isn't bought while a cheaper one sells at its regular price; for `solo`, only the preferred product; for `variety`, the qualifying allocated types;
  - spread within a pool: shares, w and Q renormalised over the pool; the stock-up phase counts the normal phase's units; a unit that doesn't fit stops allocation, with no skipping to a smaller pack;
  - `solo`: the preferred product, whatever else was bought since, by the user or the agent; an unavailable preferred product falls back to the first candidate in the cheapest order for the need only; an `on_sale` `solo` tag without it is `unavailable`; never switches for price;
  - preferred-product guess, first run only: an agent-set `cheapest` tag without an `on_sale` mode, with n ≥ 3 and ≥ 2 products, whose favourite has a share above 40 % and a paid price above the others' mean → `solo` with the favourite; a share of exactly 40 %, a favourite that is the cheapest, a favourite without a paid price, no other product with one (a partly missing price only shrinks the mean), one product bought, a policy the user set, an `on_sale` mode (stored or just assessed), or events whose lines are all q = 0 → no guess; ties by recency, then SKU id; stored by the first run; a tag meeting the conditions after setup isn't guessed;
  - `variety`: shares over the last 6 choice events plus the ½-unit floor; an unchanged agent line isn't a choice event, while a removed, added or changed type is; with fixed weights w = 3:2:1, 60 runs of 1 unit each buy every type, each within 10 points of its weight; with packs of 300/150/400/200 g every type is still bought; quantity-only edits keep every type bought in the window; a type leaving the mix at the end of the window is reported; offered and incoming units count toward the rotation; a user-owned cart unit counts toward its type; ties go to the least recently bought; a sold-out or dropped type is skipped, and an `on_sale` tag skips types outside its sale pools; the cart cap applies per type;
  - PREORDER before and after T; no live data; no candidate → unavailable vs size unknown.
- **Discount and budget:**
  - no sale marker → none; a null original price → unknown, never qualifying; a marked discount of 34.9 % vs 35 %; a sale ending today vs ended;
  - a long-life `variety` tag can be topped up, by its mix shares, and a removed top-up type counts as offered; a `solo` fallback can't be topped up;
  - the budget caps only stock-up parts, ordering by saved_czk and trimming the first line that doesn't fit (a spread line by its last-allocated units); a top-up trimmed to zero keeps its normal part; normal parts, including an `always` usual batch, are never budget-limited.
- **Ownership at close:**
  - R1 adds 2 A, R2 switches to 2 B, the user adds 1 A → user-initiated;
  - a reduced agent line stays agent-only;
  - units the user adds to a top-up line count as taken stock-up;
  - a locked tag's untouched lines keep the run that proposed them as owner run, for type misses and written stock-up.
- **Edits:**
  - locks hold only while their proposal run is in the current cycle;
  - a checkout published late doesn't carry a lock into the next cycle;
  - edits survive a crash;
  - editing one type of a `variety` tag locks the tag, and re-runs leave its other types untouched; a later edit of another type, detected by a later run of the cycle, still points at the run that proposed it, so its removal is a type miss.
- **Crash recovery:**
  - an open cycle; a closed cycle;
  - a crashed reduction keeps its ownership;
  - a user edit made while the run was crashed, on a SKU the run didn't mean to change, is detected as an edit;
  - a confirmed write is stored at once: after a crash, the user's later removal of that line is detected as an edit, the line counts as added (§6.12) and its top-up starts the wait;
  - a skip just before writing, then another confirmed write, then a failure: recovery keeps the skipped SKU at before_qty with its previous ownership, so the user's units stay the user's and the tag stays locked;
  - a failure after the write-ahead commit, before any write, leaves the run `writing`; a failure before it, `failed`;
  - cycles are resolved before recovery.
- **Writes:**
  - three consecutive runs with unchanged inputs make zero writes and keep the agent's ownership;
  - a SKU changed before the writes skips its whole tag and becomes an edit, including a line that was absent before; only tags the run means to write are checked, so no SKU gets two `run_edits` rows; the skips are stored before the first write;
  - a line the run wrote and the user changed before the final re-read keeps after_qty at its target, so the next run detects the edit;
  - no blind retry;
  - a per-SKU error doesn't stop the other writes.
- **Checkout states:**
  - checkout pending needs an owned SKU, makes the run dry with only its banner (no plan), persists until the order appears, and lapses after 24 hours, when the missing lines become edits;
  - checkout in progress needs an owned SKU of the run before the order and an order first sighted by this run; a re-run after that order was recorded runs normally.
- **Cycles:** a run that starts exactly at an order's placement belongs to the next cycle, except the order's reference run, which always belongs to the cycle it closes (a crashed run that wrote the cart and supplies an inferred placement is matched to that order); an inferred placement uses only runs that started before the order's first sighting and after every placement known by then, a crashed run counting by its start, and later runs can't move it; two orders first sighted together without `orderTime` never share a reference run, and a run belongs to at most one closed cycle.
- **Sync:** overlap with the cache; the first start date is the earlier of the newest cached order's order and delivery dates; forward paging after more than 15 unsynced orders, including a page that ends mid-day (the overlap is deduplicated); `date_to` exclusive; an empty cache; a start date that doesn't advance fails the run; the slot end is stored.
- **Tags:**
  - one product tag and one storage tag per SKU, none when ignored;
  - `tags rename`, `tags merge` and `tags set` keep `tag_id`, preferences and history;
  - `tags apply` rejects, without `units_per_pack`, text that doesn't parse in the base unit (another unit, or none), except a weighed SKU in a `pcs` tag; rejects a weighed SKU in an `l` or `m` tag; and rejects a different `policy`, `base_unit` or `stockup_mode` for an existing tag;
  - `tags apply` and `tags set` reject a stock-up mode for a short-life tag; `tags merge` keeps INTO's mode, or takes FROM's when INTO has none;
  - every writing `tags` command records its `--source`;
  - preferred product: exactly the `solo` tags have one, of their own SKUs; `tags set --preferred` makes a tag `solo`, and `--policy solo` without one is rejected, as is `--preferred` with another `--policy`; another policy clears it; `tags apply` with `preferred: true` creates it for a new tag, two such entries for one tag or one with `ignore` are rejected; on an existing tag the agent can't change it and the user can; a user-set one makes its SKU's assignment the user's, and agent-source policy changes, moves, ignores and merges that would drop it are rejected, judged on the transaction's end state; the preferred SKU leaving its tag turns the tag `cheapest`, with a report, also when it becomes another tag's preferred product (a split) or two tags swap theirs in one command; `tags merge` keeps a user-set preferred product over an agent-set one and reports every one it drops;
  - a guessed preferred product stays through later purchases and taggings: it is unavailable for weeks, fallbacks make another product the most bought, a new SKU joins the tag, and when it returns it is bought again;
  - an engine-ignored SKU that gets a paid line returns to waiting for tags.
- **Preferred products:**
  - the 1 → 2 migration, on a real version-1 database with `solo` tags, a never list, user-source rows and stock-up modes: everything survives; each `solo` tag gets its largest-share SKU, and one without a window purchase its most recently bought; the CHECKs hold; reopening is a no-op; a failure leaves version 1; the next runs' "Check these tags" lists every `solo` tag, except those whose preferred product the user has named since, until a non-dry run finishes normally;
  - a preferred product that isn't a candidate: the line records `fallback_from`; a reminder naming it, with its own availability text, shows from the start of the streak in the first 3 normally finished runs that buy the tag without it or find the tag `unavailable` (a run that doesn't buy the tag uses up none), then only in `status`; buying it again ends the streak; an `on_sale` `solo` tag's `unavailable` row shows its availability; a preferred product never bought is a candidate;
- **Outputs:** a first run's `needs_tags` lists co-purchases with untagged products; differing order-line texts are listed; `tags apply` names at most 10 tags; "bought twice in 6 months" names the window; the setup report is written to its file and `summary_md` is the compact version with the link; `data.dry_run` runs need no count reminder.
- **Plumbing:**
  - the account note;
  - the access token survives from `login` to the next command; a refresh answer without a refresh token keeps the stored one;
  - allowlist refusal;
  - result unwrapping and `is_error`;
  - the empty upcoming answer;
  - per-ID retry, and a non-candidate on missing data.

13.2 **Fixture tests.** A fake MCP client serves recorded, anonymised responses (delivered and upcoming orders, product details, cart, timeslots, user info) and drives `run` end to end. Assert the plan, the exact writes and the summary sections. Scenarios:
- failed upcoming-order read;
- missing live price;
- exactly 35 % discount;
- preferred product sold out (a `solo` tag falls back to the cheapest, and the summary's reminder, under "Reminders", names the preferred product and its availability);
- the delivery date: day 0's slot read fails and day 1 has a slot → T = day 1, "earliest slot", no warning; every day read but none has a slot → today + 1 with "no free delivery slot in the next 4 days"; some reads fail and none has a slot → today + 1 with "couldn't read the delivery slots for N of the next 4 days"; a transport failure on day 0 → no further day read;
- a `variety` tag with one type sold out;
- an `assorted` tag with a deep sale on two of its SKUs;
- an `on_sale` tag due without a sale (`wait_sale`); an `on_sale` tag topped up on a sale, then re-run in the same cycle (the line is kept, zero writes), then, after the order, re-run during the same sale and during a second deep sale of the tag that started meanwhile (no top-up until the first sale ends);
- a crashed run that wrote the cart, followed by a checkout whose upcoming order has no `orderTime` (the order is matched to the crashed run);
- an `always` tag due during a deep sale (the same quantity as without the sale);
- the cart and a new upcoming order showing the same items right after checkout, i.e. checkout in progress;
- PREORDER bread for a Sunday delivery;
- a sale ending today;
- a cancelled order whose items were re-added to the cart;
- the `error` envelope: a failed read reports its stage, `wrote = false` and at least one suggested action; a failure after a write (e.g. the final cart re-read) reports `wrote = true`, and a failed checkout-link read is only a warning;
- a crash between two writes;
- a first run whose full setup report goes to its file and whose compact `summary_md` links to it, both grouped by storage tag, showing a guessed `solo` tag with its evidence and ending with the tag check; a later run after the agent tagged new products and the user corrected another tag, whose "Check these tags" lists only the agent's, grouped by storage tag;
- a writing run whose summary lists no added lines, but one line for an `on_sale` tag topped up on a sale;
- product details failing for one agent-owned SKU (its tag is left as it is) and for every ID (the run fails before writing).

13.3 **Closed-loop simulation.**
- **Setup:** a synthetic household with known per-tag consumption — fresh tags, two weighed tags whose pieces vary ±15 % in weight (one counted in kg and eaten by weight, one counted in pieces and eaten by the piece), a `variety` tag, an `always` tag, and an `on_sale` tag with a 40 % sale every 6 weeks that lasts 2 weeks. It runs 104 weekly cycles after 26 weeks of the user's own purchases, through the real engine against a fake Rohlik. The household eats a delivery only from its delivery date. Random draws (piece weights, noise) come from one stream per tag and purpose, so adding a tag doesn't change another tag's draws.
- **Simulated user behaviour:**
  - (A) accepts every agent line and buys nothing else;
  - (C) like A, but whenever his real stock won't last to the next delivery, he raises the line, or adds one, to what he needs until then. Having gone without since his last cart check is itself such a case: he then raises the line, or adds one, to what he needs until then plus what he went without, so a surplus already in the line counts toward it, and a trim never takes the line below that. In the history weeks his own order is that check. And whenever the agent's line would leave him above the stock bound below, he trims it to fit;
  - (D) like C, plus one unit of the tag every 5 weeks regardless of stock;
  - (E) like C, with consumption doubling at week 52;
  - (G) like C, with consumption halving at week 52, and deleting every agent line of the tag while his real stock covers 2 weeks. When a paused tag runs out, he buys it himself;
  - (H) like A, for an `always` tag eaten at 2 units a week, with sales that stop at week 30: its quantity must not depend on sales.
  - (V) like A, for a `variety` tag of 3 types eaten from one slot at 3 units a week in total, starting from a 3:2:1 mix; shortage days count on the slot's total;
  - (V-C) like V, but the user adds 1 unit to a line in half the orders;
  - (V-G) like V, but the user deletes one type whenever it is offered, and raises and trims the other types' lines as in C.
- **Assertions,** per tag and per 26-week block after the first:
  - units bought are within −20 %/+30 % of true consumption (in E and G, from the second block after the change);
  - at most 3 shortage days per block, or 10 in the block where E changes; a long-life tag's 12-month window adapts more slowly, so in E it may have up to 10 in each of the two blocks after the change. A weighed tag counted in kg is exempt (the user's choice): with no margin its weeks end within grams of empty, so its shortage days are reported, not asserted, while its units bought still are;
  - where the user trims (C, E, G and V-G), average stock is at most max(1.5 cycles, 1 unit + 0.5 cycle) for short-life tags, the usual batch + 1.5 cycles for `always` tags, and MAX + 1 cycle for `on_sale` tags. A reset assumes the shelf empty when the user's own order arrives, so a buffer he keeps beyond that stays invisible to the engine (measured: about 1.4 cycles), and once consumption falls one whole unit can outlast 1.5 cycles;
  - where he never trims (A, H, V and V-C), no stock bound is asserted. Nothing pulls the learned rate back to true consumption, so an upward error persists and stock creeps: a rate learned slightly high from his own history, or an accepted round-up. His trims correct it, because a trimmed line counts (§6.8);
  - no tag is paused in A;
  - which tags each behaviour asserts: D asserts only the shortage bound, since impulse extras raise the learned rate and grow stock until the user trims it (§10.2 step 4), short-life and long-life alike; the `on_sale` tag is asserted in A, C and G, not in E, where its MAX rises only when the user raises a top-up (§6.10);
  - in V, each type's share of the units bought stays within 10 points of the 3:2:1 starting mix, and every type is bought in every block;
  - in V-C, every type with at least 10 % share in the starting mix is still bought in every block;
  - in V-G, the tag is never paused, and the deleted type is dropped within 2 offers.
- **Reported, not asserted:** per behaviour and block, the simulated user's corrections — raised lines, trimmed lines, lines he added himself, and buys of paused tags — so the correction workload is visible; and the shortage days of the weighed tag counted in kg.
- **Why §6.8–§6.10 have their shape:**
  - In simulations of single-unit tags over 4 years, resetting the projection on every order made an `always` tag over-buy by 23–38 %, with stock growing without bound, and rounding top-ups down decayed an `on_sale` tag to nothing within about two years under A, while rounding up held it at 0.92–1.15 of consumption.
  - Those simulations bought perfectly regularly, which hid a flaw: replaying the past at today's rate decays purchases to zero once the learned rate wobbles. With per-day rates (§6.9), noisy consumption of 0.3–3.3 units a week stayed at 0.99–1.05 of consumption under A.
  - Without resets, a tag whose consumption doubled needed the user's correction on every order; with them, about 20 corrections in the block of the change and none after.
  - Pure averages (the average batch at the average gap, with no running balance) bought 60–119 % of consumption: whole units and weekly orders bias them, and nothing remembers the error.
  - Under accept-all, nothing anchors the learned rate. In the closed loop, rice eaten at a fractional rate was bought at 1.10 of consumption and grew from 4.5 to 7.2 cycles of stock in two years, the `on_sale` coffee grew from 9.7 to 14.1 cycles as its ratio rose to 1.23, and one near-tie round-up (a need of 2.001 bought as 3) kept a cheese tag at 1.17 of consumption. Counting unchanged agent lines at their planned need would stop the round-up ratchet but not an inherited bias, and would set the user's lines apart from the agent's; the user chose to correct by trimming.
  - With no margin, a weighed tag counted in kg ends most weeks within grams of empty. A rate learned 1–4 % low from the weights of past deliveries is never pulled back up (as above), so the day before a delivery often runs a little short, though the tag is bought at 0.95–1.08 of consumption. Over 20 random draws, more than 3 shortage days in some half-year happened for a tag of one piece a week in 7 draws under accept-all (worst 13) and in 10 with a user who adds back what he went without (worst 6); for a potato-like tag of about 8 pieces a week, in 18 (worst 18) and 4 (worst 5). Counted in pieces, a weighed tag never had more than 1. A single draw misleads here.
  - A half-day margin for kg-weighed tags fixed the potato-like tag (0–2 of 20 draws), but under accept-all a one-piece tag then carried a whole extra piece (2 to 3 weeks of stock), and a correcting user trimmed the potato-like tag about 16 times in 18 months instead of 4. So loose produce is counted in pieces (§10.4), kg-weighed tags keep no margin, and their shortage days are reported, not asserted (the user's choice).

13.4 **Backtest** (`backtest`, also used by the setup report).
- **Data:** replay the cached history, or `--csv DIR` with the dataset of §15 loaded into an in-memory database, each order evaluated as of its delivery date.
- **Conventions:**
    - every delivered order delivered at least 183 days after the first one is evaluated at e = T = its delivery date (the CSV has no order times), using the orders strictly before it, at tag level only;
    - P = the tags that would be **added**: due, n ≥ 3, not paused. Suggestions are reported separately. In the backtest only, a tag the model would add that the order lacks counts toward its pause heuristic (the engine's real miss rule needs agent cart lines, which a CSV replay has not). The CSV's sale columns are empty, since Rohlik's order lines carry no sale markers, so every long-life tag is treated as `always` and only the due signal is measured, not quantities or top-ups;
    - an order's spend = the sum of its line prices.
- **Metrics,** macro-averaged over orders, with A_k = the tags in O_k:
    - proposed tags per order;
    - precision, same order = |P ∩ A_k| ÷ |P|, skipping orders with an empty P;
    - precision, this or next order = |P ∩ (A_k ∪ A_{k+1})| ÷ |P|;
    - recall, lines = |P ∩ A_k| ÷ |A_k|;
    - recall, spend = the spend of O_k's lines whose tag ∈ P ÷ O_k's spend.
  If no order qualifies, say that more history is needed.
- **Units:** native units by default; `--raw-units` uses size 1 for every SKU (raw sale quantities), to separate unit effects from model effects.
- **CSV loader:**
    - tag names are lowercased and cut to 64 characters (one name in the provided dataset has 65);
    - sizes come from `textual_amount` by §6.2; `products.csv`'s `base_unit` and `base_amount_per_pack` are informational;
    - a tag's base unit is kg if any of its SKUs is weighed, else the majority unit of its parsed sizes. The provided dataset's tags predate §10.4's rule for loose produce, so the loader keeps every weighed tag in kg;
    - a tag whose SKUs don't share one unit is loaded in raw sale units;
    - `storage_class` maps to storage tags: `chilled-dairy` and `chilled-other` → `chilled`, `dry-canned` → `dry`.
- **Acceptance** on the provided dataset:
    - report all metrics; they SHOULD be at least the baseline minus 3 points;
    - the baseline comes from the reference script `proxy_backtest_v11.py` (raw sale quantities) on the provided 44-order dataset: 13.5 tags per order, precision 48 % (same order) / 65 % (this or next order), recall 28 % of lines and 28 % of spend;
    - a larger shortfall MUST be explained in the delivery notes, together with the `--raw-units` result.

13.5 **Determinism.** Two runs on the same fixtures and `--now` produce byte-identical `data` JSON, except `data.report_path`, which names the data directory; the report files themselves are byte-identical.

13.6 **Live acceptance**, with the user after review:
1. on a machine without uv, the agent stops at the dependency stage and suggests the user-local install; once the user agrees, it installs uv and continues;
2. `login`;
3. the first `run` is a dry run with zero writes, and its setup report is grouped by storage tag and ends with the tag check;
4. the second `run` writes the cart; its summary lists no added lines, and the agent asks the user to check the counts in the cart;
5. an immediate re-run makes zero writes;
6. a line the user removed stays removed across two more re-runs;
7. after removing the same agent line in two orders, the tag shows in `status` as paused;
8. an existing database migrates to version 2, the next run lists its `solo` tags with their preferred products, and after "make Pohodová L my eggs" the next writing run buys Pohodová L.

13.7 **Fixture recorder.** A committed recorder and anonymiser (`tools/record_fixtures.py`) captures read-only responses and strips names, e-mails, addresses, phones, courier and payment fields, so fixtures can be regenerated.

## 14. Verification items

Resolve each with read-only calls before relying on it, and record the findings in `reference/rohlik-api-notes.md`. Write calls happen only in an explicit non-dry `run`.
- **V1** Server-side tool names and input schemas (`tools/list`). *Resolved from the tool schemas, 2026-10-07; confirmed live 2026-10-26 (58 tools, all allowlisted ones present). Confirm with `tools/list` at startup.*
- **V2** Login:
    - confidential-client registration with auth code + PKCE and a fixed loopback port;
    - then the device grant;
    - whether refresh tokens are issued; token lifetimes; the minimal working scopes.
  *Resolved 2026-10-26 (§4.2): the first strategy works, with a refresh token, 8-hour access tokens and scopes `openid email roles`; the device grant wasn't needed. A refresh in a new process needs the persisted token endpoint (§4.2); a live refresh after the 8 hours is still to be observed.*
- **V3** Product details. *Resolved 2026-10-07 (§4.5).* A live sold-out product (2026-10-26) is `UNAVAILABLE`, with `maxAvailableAmount` 0, the reason "Vyprodáno." and no `expectedReplenishment`. Still to confirm: PREORDER.
- **V4** Cart. *Resolved 2026-10-26 with a probe on 2 products not in the cart:* `update_cart_item` on an absent line fails ("Cart item not found"), so absent lines are added with `add_items_to_cart`; weighed products are added in pieces, and a second add increments the line; `add_items_to_cart` can answer `success: true` and still list SKUs in `items_failed_to_add` (§4.4). The cart-total field is `data.totalPrice`.
- **V5** Orders: `orderTime` on upcoming orders. There was no upcoming order to check on 2026-10-07. If upcoming orders carry it, delete the placement inference and the reference run (§6.6), with their tests. *The warranty field names (`pieces`, `alreadyClaimedPieces`) and piece quantities of weighed lines were resolved.*
- **V6** The minimum-order fields in `get_cart`. *Resolved (§4.5); confirmed live 2026-10-26.*
- **V7** `fetch_orders` paging. *Resolved:*
    - limit-only calls return the newest first; date ranges return the oldest 15 first; `date_to` is exclusive;
    - date ranges filter on the order date: [2026-10-06, 2026-10-26) returned an order placed on 10-25 and delivered on 10-26 (§6.1's overlap makes either harmless).
- **V8** Adding a cart to a placed order is out of scope (§1.4). Check only whether it creates an order with its own ID; if it does, it is an ordinary order and the checkout-pending lapse (§6.6) never comes into play.
    - *The four recorded orders whose totals were 3–31 % of their line sums are credit-paid (`creditsUsed`), not suborders.*
    - No suborder was seen in 44 orders.
- **V9** Checkout publication lag: how soon a checked-out order appears as upcoming. If it is immediate, delete the checkout-pending rule (§6.6), its banner and its tests.
- **V10**:
    - whether a delivered order leaves the upcoming list and appears among delivered orders at the same moment;
    - *resolved:* the fields of `order_type="cancelled"` (§4.5), and that cancelling re-adds the items to the cart by default.
- **V11** When Rohlik fixes an order's prices: at checkout, or at delivery. §6.14 assumes checkout, so a sale counts if it ends after today. Check it on an order whose sale ended between checkout and delivery; if the price is fixed at delivery, require the sale to last through T.

## 15. Deliverables

```
rohlik-replenish/
  SKILL.md
  README.md                      installing uv, login, commands, data directory, upgrading (§12)
  replenish.py                   entry point with a PEP 723 header (bounded pins)
  replenish.py.lock              uv lock --script
  engine/                        modules
  reference/tagging.md
  reference/rohlik-api-notes.md  V1–V11 findings
  tools/record_fixtures.py       fixture recorder + anonymiser
  tests/  tests/fixtures/
```
- **Tests** MUST run on the locked dependency versions. Export the lock with `uv export --script replenish.py` to a pinned requirements file (committed, and regenerated whenever the lock changes), then run `uv run --with pytest --with-requirements <that file> pytest`. Passing `--with-requirements replenish.py` would resolve versions afresh and ignore the lock.
- **Backtest dataset**, used by `--csv DIR` and provided with the spec together with `proxy_backtest_v11.py`: all 44 orders delivered from 2025-10-07 to 2026-10-26, with 1,040 lines and 363 products.
  - `purchases.csv`: order_id, delivered_date, product_id, product_name, quantity, unit, unit_price_czk, line_price_czk, sale_indicator, original_price_czk, textual_amount, weighted;
  - `products.csv`: product_id, product_name, tag, storage_class, base_unit, base_amount_per_pack, orders_count, total_spend_czk;
  - `orders.csv`: order_id, delivered_date, total_price_czk, line_count. `total_price_czk` is the amount paid **after credits**; four orders were mostly paid with credits.
- **Column meanings:**
  - `tag` is the SKU's product tag, and `storage_class` its storage tag;
  - `quantity` is in sale units (pieces for weighed products);
  - `textual_amount` is that delivery's text (per piece for weighed products).
