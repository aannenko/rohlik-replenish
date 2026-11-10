# Rohlik MCP API notes (V1–V11)

Findings behind the engine's assumptions (spec §4.5, §14). Live checks on **2026-10-26** used the engine's own MCP
client (`engine/mcp_transport.py`) against `https://mcp.rohlik.cz/mcp`, read-only except the V4 cart probe, which used
two products not in the cart and restored the cart exactly (it was empty before and after). No order was placed,
changed or cancelled, and no checkout, timeslot, claim, credit, address or payment tool was called.

| Item | Status | Finding |
| --- | --- | --- |
| V1 tools | resolved | `tools/list` (engine client, after login) lists 58 tools including every tool the engine uses: `fetch_orders`, `get_product_details`, `get_cart`, `get_timeslots_checkout`, `get_user_info`, `get_checkout_link`, `add_items_to_cart`, `update_cart_item` (the eight on the allowlist; the engine removes a line with `update_cart_item` quantity 0). Every tool takes a `context` string. `fetch_orders.order_type` ∈ delivered/upcoming/both/cancelled; `update_cart_item.quantity` is an integer ≥ 0; `get_product_details.include_description` defaults to true. The engine checks the allowlisted names on every `run` and stops with stage `tools` if one is missing. |
| V2 login | resolved | Dynamic client registration as a **confidential** client works. The engine asks for `client_secret_basic`; Rohlik registered the client with **`client_secret_post`** (RFC 7591 allows the substitution; the SDK follows the registered method). Registered scopes `openid offline_access roles profile email`; granted `openid roles email`. Authorization code + PKCE `S256` with the fixed loopback redirect `http://127.0.0.1:<port>/callback` works. A **refresh token is issued**. The access token lives **28,799 s (8 h)**; the refresh-token lifetime is not reported. The device grant is not needed and not implemented. |
| V3 product details | partly | Sold-out products (3 checked): `availabilityStatus: UNAVAILABLE`, `maxAvailableAmount: 0`, `availabilityReason: "Vyprodáno."`, `maxBasketAmountReason: "AVAILABLE"`, **no** `expectedReplenishment`. PREORDER was not observed. Extra fields seen: `productId`, `prices.salePriceStyle`. The tool description now also mentions `xtraMembershipRequired` and `prices.xtraMemberPrice` (an Xtra-only offer the customer isn't receiving); the engine ignores them and prices with `salePrice` when its sale counts, else `originalPrice`. |
| V4 cart writes | resolved | `update_cart_item` on a product **not** in the cart **fails** ("Cart item not found (product_id=…)"), so absent lines use `add_items_to_cart`; after a write error the engine re-reads the cart and retries with `add_items_to_cart` only if the re-read shows no line, else (line present, or the re-read failed) with the idempotent `update_cart_item`. Weighed products are added **in pieces**: quantity 1 → one piece, the cart line shows `quantity: 1`, `textualAmount: "cca 170 g"`, `maxBasketAmount: 50`; a second add of 1 makes it 2 (adds to the line). `remove_cart_item` removes the line (not allowlisted). `add_items_to_cart` answers `success: true` with an `items_failed_to_add` list for products it could not add; the engine treats a non-empty list as a failed write. |
| V5 upcoming `orderTime` | open | No upcoming order existed on 2026-10-26. The placement inference and reference run (§6.6) stay, with their tests. |
| V6 minimum order | resolved | `get_cart.data`: `totalPrice`, `totalSavings`, `minimalStandardOrderPrice` (749), `minimalDeliveryPointOrderPrice` (500), `minimalOrderPrice` (749), `submitConditionPassed`. `get_user_info` also has `minimum_order_value` (unused). |
| V7 paging | resolved (spec) | Not re-tested beyond the first sync: a limit-only call returned the newest orders, and forward paging from today − 365 days cached all 42 orders of the last 12 months. |
| V8 cart added to a placed order | open | Out of scope (§1.4); not tested (it would modify an order). |
| V9 checkout publication lag | open | Needs a real checkout; not tested. The checkout-pending rule stays. |
| V10 delivered vs upcoming | open | Needs a delivery in progress; not tested. `order_type="cancelled"` listed one cancelled order (2019), with `id`, `orderTime`, `itemsCount`, `state` only. |
| V11 price fixed at checkout or delivery | open | Needs a real order; not tested. |

## Response shapes (verified)

- Every text result repeats the payload wrapped as `{"result": …}` after the first JSON value; some tools (e.g.
  `get_user_info`) also return `structuredContent`. The engine prefers `structuredContent`, else parses only the first
  JSON value of the first text block, and unwraps a lone `{"result": …}`.
- `get_user_info`: top-level `success`, `id`, `xtra.limits.orders_without_min_remaining` (plus personal fields the
  engine never stores or logs).
- `get_checkout_link`: `{"checkout_link": "https://www.rohlik.cz/pay-by-link?origin=MCP"}`.
- `fetch_orders` (delivered): `orderTime` like `2026-10-25T20:00:41.000+0100`; `deliverySlot.timeRange` like
  `2026-10-26 08:00-09:00`; `items[]` with `id`, `name`, `unit`, `textualAmount`, `quantity`, `totalPrice` (a number)
  and `currency`; `priceComposition.*` as `{amount, currency}`; `warrantyInfo.enabledData/disabledData[]` with
  `productId`, `inventoryId`, `pieces`, `alreadyClaimedPieces`, `remainingPieces`, `weighted`, `totalWeight`,
  `pricePerPiece` and `total` as strings.
- `fetch_orders` (upcoming, none): `{"success": true, "data": null, "message": "No upcoming orders found…",
  "is_empty": true}`.
- `get_cart` (empty): `data.items` is `{}`, `totalPrice` 0.
- `get_timeslots_checkout(timeslots_day)`: `data.availabilityDays{date, slots{"<hour>": [{slotId, timeWindow, slotType,
  capacityPercent, price, …}]}}`; a free slot has a `slotId`, a full one `capacityPercent: "NO CAPACITY"` and none.
  **Late in the day, day "0" can fail** (live, 2026-11-01): at 14:42 it answered with same-day slots, but at 20:04 it
  answered with the tool's usage hint as its message ("All times are in 24-hour format with a 12-hour hint in
  parentheses. IMPORTANT: …") and no `availabilityDays`, which the engine reads as a failed call.
    It reads each day on its own (§6.5): a failed day is skipped, and only a transport failure that
  outlasts its retries ends the search. The warning says which: "no free delivery slot in the next 4 days", or
  "couldn't read the delivery slots for N of the next 4 days".

## OAuth metadata (public, read 2026-10-26)

- `https://mcp.rohlik.cz/.well-known/oauth-protected-resource/mcp`: resource `https://mcp.rohlik.cz/mcp`,
  authorization server `https://identity.rohlik.cz`, scopes `openid email roles`. The root variant and
  `mcp.rohlik.cz/.well-known/oauth-authorization-server` answer 404.
- `https://identity.rohlik.cz/.well-known/oauth-authorization-server` (and `openid-configuration`): token endpoint
  **`https://identity.rohlik.cz/oauth2/token`**, authorization `/oauth2/authorize`, registration `/connect/register`;
  token auth methods include `client_secret_basic`, `client_secret_post` and `none`.
- `https://mcp.rohlik.cz/token` answers 401: it isn't a token endpoint.

## SDK notes (mcp 2.3.0)

- The OAuth provider is an `httpx2.Auth`; the transport takes `streamable_http_client(url, http_client=…)`.
- The provider loads stored tokens without their expiry, so it would send an expired token, get a 401 and start a
  new browser flow instead of refreshing; the engine's provider subclass restores the stored expiry so the refresh
  token is used first.
- A fresh provider knows no server metadata, and the SDK then sends a refresh to `<MCP origin>/token`
  (`https://mcp.rohlik.cz/token`, which answers 401), so every new process would lose the session once the 8-hour
  access token expired. The provider subclass stores the metadata discovered at login in `auth.json`
  (`server_metadata`: public, never secrets) and restores it, so the refresh goes to the real token endpoint.
  `tests/test_auth_refresh.py` covers it through the real SDK stack against an in-memory Rohlik.
- A keyring that can't be read or written raises `KeyringFailure`; `run` reports it as an `error` envelope at stage
  `auth`, also when it happens inside the SDK while storing refreshed tokens.
- A refresh answer without a refresh token (a server that doesn't rotate them) keeps the stored one (§4.2): only a
  rejected refresh ends the session. `TokenStore.set_tokens` never deletes the stored refresh token itself; a new
  `login` clears the tokens first. Covered through the SDK stack and at the storage boundary in
  `tests/test_auth_refresh.py`.
- A network or auth failure inside the transport ends the whole session (it surfaces as an exception group when the
  session closes), so the engine runs the session on its own event-loop thread and reconnects on the next call.
- HTTP 429/5xx reach the client as JSON-RPC `INTERNAL_ERROR` "Server returned an error response"; read calls retry
  them with backoff.
