"""Rohlik MCP client wrapper (spec §4): allowlist, retries, result parsing, call log.

The wrapper is synchronous; a transport (the real MCP connection or the fixture fake) carries the calls.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .constants import (ALLOWED_ORDER_TYPES, ALLOWED_TOOLS, CALL_CONTEXT, CALL_TIMEOUT_S, DETAILS_BATCH,
                        FETCH_LIMIT, READ_BUDGET_S, READ_RETRIES, READ_TOOLS, WRITE_TOOLS)

log = logging.getLogger("rohlik_replenish.client")


class AuthRequired(Exception):
    """No valid session: the user has to run `login` (routine, not an error)."""


class NotAllowed(Exception):
    """A tool outside the allowlist (§4.3); raised before anything is sent."""


class TransportError(Exception):
    """Network failure, timeout, HTTP 429/5xx. `retryable` applies to reads only."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class ToolFailure(Exception):
    """The tool answered with is_error, success: false, or without its expected key (§4.4)."""

    def __init__(self, tool: str, message: str):
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.message = message


class KeyringFailure(Exception):
    """The OS keyring, which holds the tokens and the client secret (§4.2), couldn't be read or written."""


KEYRING_ACTIONS = ["the OS keyring that holds your Rohlik login couldn't be used: run again in a minute",
                   "if it keeps failing, check that Windows Credential Manager (or your OS keyring) works, then run "
                   "`login`"]


@dataclass
class RawResult:
    structured: Any = None
    texts: list[str] = field(default_factory=list)
    is_error: bool = False


class Transport(Protocol):
    def call(self, tool: str, args: dict, timeout: float) -> RawResult: ...

    def list_tools(self) -> list[dict]: ...

    def close(self) -> None: ...


@dataclass
class CallRecord:
    seq: int
    tool: str
    args_json: str
    duration_ms: int
    outcome: str


def extract_payload(raw: RawResult) -> Any:
    """Prefer structuredContent; else only the first JSON value of the first text block; unwrap a lone
    {"result": ...}."""
    payload = raw.structured
    if payload is None:
        if not raw.texts:
            raise ValueError("empty result")
        text = raw.texts[0].lstrip()
        payload, _ = json.JSONDecoder().raw_decode(text)
    if isinstance(payload, dict) and len(payload) == 1 and "result" in payload:
        payload = payload["result"]
    return payload


def _message(payload: Any, default: str) -> str:
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            if payload.get(key):
                return str(payload[key])
    return default


def _check_envelope(tool: str, payload: Any) -> Any:
    """Per-tool envelope rules (§4.4). Returns the useful part of the payload."""
    if isinstance(payload, dict) and payload.get("success") is False:
        raise ToolFailure(tool, _message(payload, "success: false"))
    if tool == "fetch_orders":
        if not isinstance(payload, dict):
            raise ToolFailure(tool, "unexpected payload")
        if isinstance(payload.get("orders"), list):
            return payload["orders"]
        if payload.get("is_empty") is True or ("data" in payload and payload.get("data") is None):
            return []
        raise ToolFailure(tool, _message(payload, "expected key 'orders' missing"))
    if tool == "get_cart":
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ToolFailure(tool, _message(payload, "expected key 'data' missing"))
        items = data.get("items")
        if not isinstance(items, dict):
            raise ToolFailure(tool, "cart items are not a map")
        for key, line in items.items():
            # §4.4: an unreadable quantity is never taken as 0, or a user's units could be overwritten
            if not isinstance(line, dict) or _quantity(line.get("quantity")) is None:
                raise ToolFailure(tool, f"cart line {key} has no readable quantity")
        return data
    if tool == "get_product_details":
        if not isinstance(payload, dict):
            raise ToolFailure(tool, "unexpected payload")
        return payload
    if tool == "get_timeslots_checkout":
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or "availabilityDays" not in data:
            raise ToolFailure(tool, _message(payload, "expected key 'data.availabilityDays' missing"))
        return data
    if tool == "get_user_info":
        if isinstance(payload, dict):
            if payload.get("id") is not None:
                return payload
            data = payload.get("data")
            if isinstance(data, dict) and data.get("id") is not None:
                return data
        raise ToolFailure(tool, _message(payload, "expected key 'id' missing"))
    if tool == "get_checkout_link":
        url = _find_url(payload)
        if url is None:
            raise ToolFailure(tool, _message(payload, "no checkout link in the answer"))
        return url
    if tool == "add_items_to_cart" and isinstance(payload, dict) and payload.get("items_failed_to_add"):
        # verified 2026-10-26: Rohlik answers success: true and lists the products it could not add here
        raise ToolFailure(tool, f"items failed to add: {json.dumps(payload['items_failed_to_add'])[:300]}")
    return payload  # writes: success not false is enough


def _quantity(value: Any) -> float | None:
    """A cart line quantity: a finite, non-negative number (numeric text accepted), else None."""
    if isinstance(value, bool):
        return None
    try:
        q = float(value)
    except (TypeError, ValueError):
        return None
    return q if math.isfinite(q) and q >= 0 else None


def _find_url(payload: Any) -> str | None:
    """The checkout link: verified shape {"checkout_link": "https://…"}; other URL-looking values as a fallback."""
    if isinstance(payload, str):
        return payload if payload.startswith("http") else None
    if isinstance(payload, dict):
        for key in ("checkout_link", "url", "checkoutUrl", "checkout_url", "link", "checkoutLink", "data"):
            found = _find_url(payload.get(key))
            if found:
                return found
        for value in payload.values():
            found = _find_url(value) if isinstance(value, (str, dict)) else None
            if found:
                return found
    return None


class BudgetExhausted(TransportError):
    """All of a run's reads share a time budget (§4.4); a read that would start after it fails the run."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False)


class RohlikClient:
    def __init__(self, transport: Transport, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, read_budget_s: float = READ_BUDGET_S):
        self.transport = transport
        self._sleep = sleep
        self._clock = clock
        self.read_budget_s = read_budget_s
        self._reads_started: float | None = None
        self.calls: list[CallRecord] = []
        self.wrote = False  # a cart write was attempted

    # ---------------------------------------------------------------- core
    def _log(self, tool: str, args: dict, started: float, outcome: str) -> None:
        shown = {k: v for k, v in args.items() if k != "context"}
        self.calls.append(CallRecord(len(self.calls) + 1, tool, json.dumps(shown, sort_keys=True, ensure_ascii=False),
                                     int((self._clock() - started) * 1000), outcome[:300]))

    def _check_budget(self, tool: str) -> None:
        now = self._clock()
        if self._reads_started is None:
            self._reads_started = now
        elif now - self._reads_started >= self.read_budget_s:
            raise BudgetExhausted(f"{tool}: Rohlik is too slow today (the run's reads took "
                                  f"{int(self.read_budget_s // 60)} minutes)")

    def call(self, tool: str, **args) -> Any:
        if tool not in ALLOWED_TOOLS:
            raise NotAllowed(f"tool '{tool}' is not on the allowlist")
        if tool == "fetch_orders" and args.get("order_type") not in ALLOWED_ORDER_TYPES:
            raise NotAllowed(f"fetch_orders order_type '{args.get('order_type')}' is not allowed")
        args = dict(args, context=CALL_CONTEXT)
        is_read = tool in READ_TOOLS
        if tool in WRITE_TOOLS:
            self.wrote = True
        attempts = 1 + (READ_RETRIES if is_read else 0)
        for attempt in range(attempts):
            if is_read:
                self._check_budget(tool)
            started = self._clock()
            try:
                raw = self.transport.call(tool, args, CALL_TIMEOUT_S)
            except AuthRequired:
                self._log(tool, args, started, "auth_required")
                raise
            except TransportError as exc:
                self._log(tool, args, started, f"transport error: {exc}")
                if is_read and exc.retryable and attempt < attempts - 1:
                    self._sleep(2.0 ** attempt)
                    continue
                raise
            if raw.is_error:
                text = raw.texts[0] if raw.texts else "is_error"
                self._log(tool, args, started, f"is_error: {text}")
                raise ToolFailure(tool, text[:500])
            try:
                payload = _check_envelope(tool, extract_payload(raw))
            except ToolFailure as exc:
                self._log(tool, args, started, f"failed: {exc.message}")
                raise
            except ValueError as exc:
                self._log(tool, args, started, f"unparsable: {exc}")
                raise ToolFailure(tool, f"unparsable answer: {exc}") from exc
            self._log(tool, args, started, "ok")
            return payload
        raise AssertionError("unreachable")

    # ---------------------------------------------------------------- reads
    def fetch_orders(self, order_type: str, *, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
        args: dict[str, Any] = {"order_type": order_type, "limit": FETCH_LIMIT}
        if date_from:
            args["date_from"] = date_from
        if date_to:
            args["date_to"] = date_to
        orders = self.call("fetch_orders", **args)
        return [o for o in orders if isinstance(o, dict)]

    def get_cart(self) -> dict:
        return self.call("get_cart")

    def get_user_info(self) -> dict:
        return self.call("get_user_info")

    def get_timeslots(self, day: int) -> dict:
        return self.call("get_timeslots_checkout", timeslots_day=str(day))

    def get_checkout_link(self) -> str:
        return self.call("get_checkout_link")

    def product_details(self, ids: Iterable[int], *, require_any: bool = False) -> dict[int, dict]:
        """Live details per ID; IDs without data are simply absent (non-candidates, §4.4).

        A batch that fails with a tool error is retried one ID at a time. A transport, 429 or 5xx failure that
        outlasts its retries fails the read instead: splitting an outage into single IDs would only multiply it.
        With `require_any`, a read in which no requested ID returns details fails too (§7.1 step 8)."""
        wanted = sorted({int(i) for i in ids})
        out: dict[int, dict] = {}
        for start in range(0, len(wanted), DETAILS_BATCH):
            batch = wanted[start:start + DETAILS_BATCH]
            try:
                out.update(_details_map(self.call("get_product_details", product_ids=batch, include_description=False)))
            except ToolFailure:
                for pid in batch:
                    try:
                        out.update(_details_map(self.call("get_product_details", product_ids=[pid],
                                                          include_description=False)))
                    except ToolFailure:
                        continue
        found = {k: v for k, v in out.items() if k in set(wanted)}
        if require_any and wanted and not found:
            raise ToolFailure("get_product_details", f"none of the {len(wanted)} requested products returned details")
        return found

    # ---------------------------------------------------------------- writes
    def add_item(self, sku: int, quantity: int) -> Any:
        return self.call("add_items_to_cart", items=[{"productId": int(sku), "quantity": int(quantity)}])

    def update_item(self, sku: int, quantity: int) -> Any:
        return self.call("update_cart_item", product_id=int(sku), quantity=int(quantity))


def _details_map(payload: dict) -> dict[int, dict]:
    out = {}
    for key, value in payload.items():
        try:
            pid = int(key)
        except (TypeError, ValueError):
            log.debug("product details: ignoring non-ID key %r", key)
            continue
        if isinstance(value, dict):
            out[pid] = value
    return out
