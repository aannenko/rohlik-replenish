"""Engine constants (spec §11). Only `stockup_min_discount` and `stockup_budget_czk` are user preferences."""

from __future__ import annotations

APP_NAME = "rohlik-replenish"

MCP_URL = "https://mcp.rohlik.cz/mcp"
CALL_CONTEXT = "Automated grocery replenishment for the account owner."
CALL_TIMEOUT_S = 30.0
READ_RETRIES = 3
READ_BUDGET_S = 600.0  # all of a run's reads share 10 minutes (§4.4)
LOGIN_TIMEOUT_S = 300.0
TOKEN_MIN_LIFETIME_S = 60.0

# §4.3 allowlist. Anything else raises before a byte is sent.
READ_TOOLS = frozenset({
    "fetch_orders", "get_product_details", "get_cart", "get_timeslots_checkout",
    "get_user_info", "get_checkout_link",
})
WRITE_TOOLS = frozenset({"add_items_to_cart", "update_cart_item"})  # a removal sets the quantity to 0 (§4.3)
ALLOWED_TOOLS = READ_TOOLS | WRITE_TOOLS
ALLOWED_ORDER_TYPES = frozenset({"delivered", "upcoming", "cancelled"})

# §6.0 history windows (days)
WINDOW_LONG = 365
WINDOW_SHORT = 183
FIRST_SYNC_DAYS = 365
MIN_HISTORY_ORDERS = 3
MIN_HISTORY_DAYS = 183
FETCH_LIMIT = 15

# §6.4 cadence
CADENCE_GAPS = 8
CADENCE_MIN = 4.0
CADENCE_MAX = 21.0
CADENCE_DEFAULT = 7.0

# §6.5 delivery date: get_timeslots_checkout days "0".."3"
SLOT_DAYS = 4

# §6.11 gates
GATE_ADD = 3
GATE_SUGGEST = 2
OVERDUE_FACTOR = 2.0

# §6.10 caps
P90 = 0.9

# §6.12 learning
MISSES_TO_PAUSE = 2
TYPE_MISSES_TO_DROP = 2
REMINDER_RUNS = 3  # §2: a reminder shows until 3 non-dry runs on or after its date have finished normally
FIT_FACTOR = 1.5  # §6.13 fit: a pack over 1.5 × what the line may take is dropped while another fits

# §6.13 variant choice
VARIETY_CHOICE_EVENTS = 6
VARIETY_ROTATION_ORDERS = 6
VARIETY_FLOOR = 0.5
SOLO_GUESS_SHARE = 0.4  # §6.13 first-run guess: the favourite's share must be above 40 %
SOLO_GUESS_MIN_SKUS = 2  # ... of at least 2 SKUs with a purchase

# §6.14 stock-up
DEFAULT_STOCKUP_MIN_DISCOUNT = 0.35
DEFAULT_STOCKUP_BUDGET_CZK = 1000.0
UNDATED_SALE_WAIT_DAYS = 7

# §6.6 checkout pending lapse (hours)
CHECKOUT_PENDING_HOURS = 24

# §7.1 product-details batching
DETAILS_BATCH = 20

# §7.1 step 6: needs_tags co-purchases (at least 2 orders, at most 8)
BOUGHT_WITH_MIN_ORDERS = 2
BOUGHT_WITH_MAX = 8

# §9: command summaries name at most 10 items
SUMMARY_NAMES_MAX = 10

# §6.0 numerics
EPS = 1e-9

STORAGE_SEEDS = (
    ("bakery", 0), ("fresh-produce", 0), ("chilled", 0), ("fresh-meat-fish", 0), ("frozen", 1),
    ("dry", 1), ("drinks", 1), ("drugstore-household", 1), ("pet", 1), ("other", 0),
)
STORAGE_TAGS = tuple(name for name, _ in STORAGE_SEEDS)
POLICIES = ("cheapest", "assorted", "solo", "variety")
BASE_UNITS = ("kg", "l", "pcs", "m")
STOCKUP_MODES = ("always", "on_sale")
REASONS = ("due", "suggest_gate", "wait_sale", "size_unknown", "basket_limit", "unavailable",
           "stockup", "budget_limit", "overdue")
LINE_REASONS = frozenset({"due", "stockup"})

SCHEMA_VERSION = 2
