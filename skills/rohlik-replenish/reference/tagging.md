# Tagging products

When a run returns `needs_tags`, every listed product needs a **product tag** (with a policy and base unit for a new
tag) and a **storage tag**, or `ignore: true`. Write them as a JSON array and run
`uv run replenish.py tags apply <file>` (`--source user` when the user decided). The engine validates everything in one
transaction: if any entry is wrong, nothing changes and it lists the problems; fix them and apply again.

```json
[
  {"sku": 1443104, "product_tag": "milk whole fresh", "policy": "solo", "base_unit": "l", "storage_tag": "chilled",
   "preferred": true},
  {"sku": 1406260, "product_tag": "milk whole fresh", "storage_tag": "chilled"},
  {"sku": 1379448, "product_tag": "greek yogurt plain", "policy": "solo", "base_unit": "kg", "storage_tag": "chilled",
   "preferred": true},
  {"sku": 1423088, "product_tag": "dishwasher tablets all-in-one", "policy": "cheapest", "base_unit": "pcs",
   "storage_tag": "drugstore-household", "units_per_pack": 80},
  {"sku": 1477733, "ignore": true}
]
```

Fields: `sku` (required); `product_tag` and `storage_tag` (required unless `ignore`); `policy` and `base_unit`
(required for a new product tag; for an existing tag a value equal to the tag's is ignored and a different one is an
error — change it with `tags set`); `stockup_mode` (optional, long-life tags only, see below); `units_per_pack`
(see "Pack sizes"); `preferred` (see "Policy"); `ignore`.

The listing (`data.skus`) gives each product its text, parsed size, how often and when it was bought, what it was
bought together with in at least 2 orders (tags, and products still waiting for tags by name), and its order-line
texts when they differ from today's.

## A tag is a need

- Two products share a tag only if the household would accept either for the **same use**.
- **Merge what substitutes; split what is bought together.** Five apple varieties in one order are five tags. Products
  of one tag bought together in two or more orders are a sign of a wrong merge (the engine reports them).
- The exceptions, made **only at the user's request**:
  - a `variety` tag, for types the household eats from one shared slot, such as sliced cheeses for sandwiches;
  - an `assorted` tag, for kinds bought several at a time where only the price matters, such as pasta shapes.
- **Same use matters.** These are separate tags: a 1 kg curd bucket (baking) and 250 g tubs (eating); 500 g breakfast
  muesli and 50 g snack bars; lasagne sheets and other pasta. A pork-and-beef mince mix and beef mince are one tag if
  they are used the same way.
- **When unsure, keep products in separate tags.** A tag that is too broad causes wrong substitutions and quantities;
  one that is too narrow may never reach 3 purchases and is then never added.

## Names

- Short lowercase English that includes the attributes that matter: type, fat %, flavour, cut, size class, decaf,
  lactose-free, and organic when it matters.
- Examples: `milk 1.5%`, `milk 3.5%`, `eggs size l`, `minced meat`, `greek yogurt plain 5%`.
- **Never a brand**, and **never a pack size**.
- A claim that must never be lost (organic, lactose-free, gluten-free) belongs **in the name**.

## Policy

- `cheapest` — the default: the cheapest available product per base unit.
- `solo` — when the specific variant matters to the household, e.g. "organic fresh milk regardless of price",
  coffee, or baby food. It buys its **preferred product**, whatever else the user buys now and then, and the cheapest
  available one only when the preferred product is unavailable (or comes in a much bigger pack than needed).
  - A **new** `solo` tag needs exactly one entry with `"preferred": true`: the product the user named, else the one
    bought most. An ignored entry can't be preferred.
  - On an **existing** tag the preferred product changes only when the user names one (`--source user`, with
    `"preferred": true` in `tags apply`, or `tags set TAG --preferred SKU`); the agent's `tags apply` can add products
    to the tag but can't change its preferred product. Purchases and prices never change it.
  - The first run also **guesses** `solo` for tags whose favourite product makes up over 40 % of the purchases and
    costs more than the others the user bought; the setup report shows each guess for the user to confirm or correct
    in its tag check.
- `variety` and `assorted` — only when the user asks for them (see above).

## Stock-up mode (long-life tags)

A tag is **long-life** when every product in it has a stock-up-eligible storage tag, else short-life. Long-life tags
have a mode: `always` (when it runs low, the usual batch is added at whatever price) or `on_sale` (topped up to the
largest purchase on a sale of at least 35 %, once per sale; otherwise only "running low — waiting for a sale").
The **first run** guesses each long-life tag's mode from the prices the user paid and shows it, with its evidence, in
the setup report's tag check for the user to confirm; tags that become long-life later (bought 3 times) default to
`always`. **Set `stockup_mode` only when the user states one**; the user can change it at any time
(`tags set TAG --stockup-mode …`, with `--source user`). Short-life tags have none (the engine rejects it).

## Base unit

`kg`, `l` or `pcs`; `m` for products Rohlik prices per metre (toilet paper, kitchen towels), so prices compare per
metre.

**Weighed products** (⚖ in the listing: sold by the piece, charged by weight) go in a `kg` or a `pcs` tag, never `l`
or `m`:

- a **`pcs` tag** when the household eats or uses each piece whole, one at a time: loose fruit, peppers and
  cucumbers, such as bananas, apples, citrus, plums and melons. Each piece then counts as 1 whatever it weighs, so a
  lighter piece doesn't make the plan short. A weighed bunch or pack of such pieces in the same tag gives its piece
  count as `units_per_pack` (a ~0.7 kg bunch of bananas: `4`);
- a **`kg` tag** for every other weighed product: vegetables cooked by weight (potatoes, carrots, onions, cabbage,
  squash), meat, fish, deli and cheese, and any tag that mixes loose pieces with bags or packs. It counts their
  actual weight.

The user can switch a tag between the two (`tags set TAG --base-unit pcs|kg --source user`); its history is then
re-counted in the new unit. After a switch to `pcs`, give a bunch or pack its piece count.

```json
[
  {"sku": 1349777, "product_tag": "bananas", "policy": "cheapest", "base_unit": "pcs", "storage_tag": "fresh-produce"},
  {"sku": 1375865, "product_tag": "bananas", "storage_tag": "fresh-produce", "units_per_pack": 4},
  {"sku": 1350963, "product_tag": "potatoes", "policy": "cheapest", "base_unit": "kg", "storage_tag": "fresh-produce"}
]
```

## Storage tag (per product)

How that product is stored: `bakery`, `fresh-produce`, `chilled`, `fresh-meat-fish`, `frozen`, `dry`, `drinks`,
`drugstore-household`, `pet`, `other`.

- Fresh dairy is `chilled`, UHT milk is `dry`, and frozen products are `frozen`.
- Fresh and frozen variants may share a product tag when the household uses them the same way (the tag is then
  short-life, because one product isn't stock-up-eligible).
- Stock-up eligible by default: `frozen`, `dry`, `drinks`, `drugstore-household`, `pet`. The user can change a storage
  tag's flag with `tags storage-set NAME --stockup yes|no`.

## Pack sizes

The engine parses the size from the product's text (`textualAmount`):

- a number with `,` or `.` decimals followed by `g`, `kg`, `ml`, `l`, `ks` or `m`, and nothing else
  (`80 mytí`, "80 washes", doesn't parse);
- `N x size` and `N× size` multiply (`6×1,5l` = 9 l, `2 x 100 g` = 0.2 kg);
- prefixes such as `cca` are ignored (`cca 190 g` = 0.19 kg);
- a weighed product's size is its weight per piece in a `kg` tag, and 1 piece in a `pcs` tag (its text, a weight,
  needs no `units_per_pack` there).

Resolve **only** what the engine reports as unparsed (`unit`/`size` null) or "in another unit": give
`units_per_pack`, a number **in the tag's base unit**, taken from the name or text (e.g. "cca 500 g" in the name of a
bunch sold per piece → `0.5` in a `kg` tag; "80 mytí" → `80` in a `pcs` tag). If it can't be determined, give
`"units_per_pack": null`; the tag is then shown as a "pack size unknown" suggestion instead of being added. The listing
shows a hint from price ÷ unit price where it can.

**Order-line texts that differ from today's** (e.g. "'2 l' in 3 orders, '500 ml' now") are shown, not resolved: a
pack may have changed size, or a text may have been wrong. Give `units_per_pack` only when the name or the user says
which is right (e.g. "2×250 ml" in the name against an old "2 l"), since packs do change size. Weighed products are
never listed this way: their line text is that delivery's piece weight.

## Non-products

Catalogues, deposits, gifts and other lines that aren't groceries → `{"sku": …, "ignore": true}`. Lines that were
always free are ignored by the engine automatically.

## Never change `source: user` assignments

Assignments and sizes the user made (`--source user`) are never overwritten by the agent; the engine refuses such an
entry. A preferred product the user named makes its product's assignment his, and the engine also refuses an agent
command that would clear or change it (another policy, moving or ignoring the product, merging its tag away). Use
`--source user` only for the user's own decisions.
