"""The real Rohlik MCP connection (§4.1, §4.2): OAuth with the official MCP SDK (2.x), secrets in the OS keyring.

The SDK is asynchronous and a network or auth failure ends its whole session (it surfaces as an exception group
when the session closes). The engine is synchronous, so the session lives on its own event-loop thread behind
`McpTransport.call`, which reconnects on the next call after a failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from .constants import APP_NAME, LOGIN_TIMEOUT_S, MCP_URL, TOKEN_MIN_LIFETIME_S
from .paths import AuthFile
from .rohlik import AuthRequired, KeyringFailure, RawResult, TransportError

log = logging.getLogger("rohlik_replenish.mcp")

CHUNK_CHARS = 1000  # Windows Credential Manager: 2,560 bytes per secret; keyring stores UTF-16


# ------------------------------------------------------------------------------------------ secrets
class SecretStore:
    """Separate OS-keyring entries under fixed names; long values split across numbered entries. A failing keyring
    raises KeyringFailure, so a run reports it as such rather than as a network or internal fault."""

    def __init__(self, service: str = APP_NAME, backend=None):
        self.service = service
        if backend is None:
            import keyring

            backend = keyring
        self._kr = backend

    def _get(self, name: str) -> str | None:
        try:
            return self._kr.get_password(self.service, name)
        except Exception as exc:  # noqa: BLE001 (backend-specific keyring errors)
            raise KeyringFailure(f"the OS keyring couldn't be read: {_describe(exc)}") from exc

    def _set(self, name: str, value: str) -> None:
        try:
            self._kr.set_password(self.service, name, value)
        except Exception as exc:  # noqa: BLE001
            raise KeyringFailure(f"the OS keyring couldn't be written: {_describe(exc)}") from exc

    def get(self, name: str) -> str | None:
        head = self._get(name)
        if head is None:
            return None
        count, _, first = head.partition("|")
        if not count.isdigit():
            return head
        parts = [first]
        for i in range(1, int(count)):
            part = self._get(f"{name}.{i}")
            if part is None:
                return None
            parts.append(part)
        return "".join(parts)

    def set(self, name: str, value: str) -> None:
        chunks = [value[i:i + CHUNK_CHARS] for i in range(0, len(value), CHUNK_CHARS)] or [""]
        old = self._count(name)
        self._set(name, f"{len(chunks)}|{chunks[0]}")
        for i, chunk in enumerate(chunks[1:], start=1):
            self._set(f"{name}.{i}", chunk)
        for i in range(len(chunks), old):
            self._delete_quiet(f"{name}.{i}")

    def delete(self, name: str) -> None:
        old = self._count(name)
        self._delete_quiet(name)
        for i in range(1, old):
            self._delete_quiet(f"{name}.{i}")

    def _count(self, name: str) -> int:
        head = self._get(name)
        if head is None:
            return 0
        count = head.partition("|")[0]
        return int(count) if count.isdigit() else 1

    def _delete_quiet(self, name: str) -> None:
        try:
            self._kr.delete_password(self.service, name)
        except Exception:  # noqa: BLE001 (keyring raises backend-specific PasswordDeleteError)
            pass


class MemoryKeyring:
    """A keyring backend for tests."""

    def __init__(self):
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service, name):
        return self.data.get((service, name))

    def set_password(self, service, name, value):
        self.data[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.data:
            raise KeyError(name)
        del self.data[(service, name)]


# ------------------------------------------------------------------------------------------ token storage
class TokenStore:
    """The SDK's `TokenStorage`: tokens and the client secret in the keyring, the registration in auth.json."""

    def __init__(self, secrets: SecretStore, auth: AuthFile, clock=time.time):
        self.secrets = secrets
        self.auth = auth
        self.clock = clock

    # sync helpers ------------------------------------------------------------------
    def access(self) -> dict | None:
        raw = self.secrets.get("access_token")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def refresh_token(self) -> str | None:
        return self.secrets.get("refresh_token")

    def access_expires_at(self) -> float | None:
        acc = self.access()
        return acc.get("expires_at") if acc else None

    def has_session(self) -> bool:
        if self.refresh_token():
            return True
        acc = self.access()
        if not acc or not acc.get("token"):
            return False
        exp = acc.get("expires_at")
        return exp is None or exp > self.clock() + 5

    def write_window_ok(self, now) -> bool:
        """Without a refresh token, at least 60 s of access-token lifetime must remain before writing."""
        if self.refresh_token():
            return True
        exp = self.access_expires_at()
        return exp is None or exp - now.timestamp() >= TOKEN_MIN_LIFETIME_S

    def clear_tokens(self) -> None:
        self.secrets.delete("access_token")
        self.secrets.delete("refresh_token")

    def clear_client(self) -> None:
        self.secrets.delete("client_secret")
        self.auth.update(client=None)

    # TokenStorage protocol -----------------------------------------------------------
    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        acc = self.access()
        refresh = self.refresh_token()
        if not acc and not refresh:
            return None
        exp = acc.get("expires_at") if acc else None
        expires_in = max(0, int(exp - self.clock())) if exp else None
        return OAuthToken(access_token=(acc or {}).get("token", ""), token_type="Bearer", expires_in=expires_in,
                          scope=(acc or {}).get("scope"), refresh_token=refresh)

    async def set_tokens(self, tokens) -> None:
        exp = self.clock() + tokens.expires_in if tokens.expires_in else None
        self.secrets.set("access_token", json.dumps({"token": tokens.access_token, "expires_at": exp,
                                                     "scope": tokens.scope}))
        if tokens.refresh_token:
            self.secrets.set("refresh_token", tokens.refresh_token)
        # §4.2: a refresh answer without a refresh token keeps the stored one; only a rejected refresh ends
        # the session. A new login starts from no tokens (login() clears them first).
        held = bool(tokens.refresh_token) or bool(self.refresh_token())
        self.auth.update(refresh_token_issued=held, access_token_lifetime_s=tokens.expires_in,
                         granted_scope=tokens.scope)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        client = self.auth.load().get("client")
        if not client:
            return None
        data = {k: v for k, v in client.items() if k not in ("port",)}
        data["client_secret"] = self.secrets.get("client_secret")
        try:
            return OAuthClientInformationFull.model_validate(data)
        except Exception:  # noqa: BLE001 (a stale registration is simply redone)
            return None

    async def set_client_info(self, info) -> None:
        data = info.model_dump(mode="json", exclude_none=True)
        secret = data.pop("client_secret", None)
        if secret:
            self.secrets.set("client_secret", secret)
        else:
            self.secrets.delete("client_secret")
        uris = data.get("redirect_uris") or []
        if uris:
            data["port"] = urlparse(str(uris[0])).port
        self.auth.update(client=data)


def _provider_class():
    from mcp.client.auth import OAuthClientProvider

    class Provider(OAuthClientProvider):
        """Restores what a fresh process needs to refresh instead of starting a browser login:
        - the stored absolute expiry, so an expired access token is refreshed before use;
        - the server metadata discovered at login. Without it the SDK sends the refresh to `<MCP origin>/token`,
          but Rohlik's token endpoint is on its authorization server (identity.rohlik.cz), so every refresh in a
          new process would fail and each run after the 8-hour access token would need `login`."""

        def __init__(self, *args, store: TokenStore, **kwargs):
            super().__init__(*args, **kwargs)
            self._store = store

        async def _initialize(self) -> None:
            await super()._initialize()
            self._restore_metadata()
            exp = self._store.access_expires_at()
            if self.context.current_tokens is not None:
                if not self.context.current_tokens.access_token:
                    self.context.token_expiry_time = 0.0
                elif exp is not None:
                    self.context.token_expiry_time = exp

        async def _handle_token_response(self, response) -> None:
            await super()._handle_token_response(response)
            self._save_metadata()

        def _restore_metadata(self) -> None:
            from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

            saved = self._store.auth.load().get(METADATA_KEY) or {}
            try:
                if self.context.oauth_metadata is None and saved.get("authorization_server"):
                    self.context.oauth_metadata = OAuthMetadata.model_validate(saved["authorization_server"])
                if self.context.protected_resource_metadata is None and saved.get("protected_resource"):
                    self.context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(
                        saved["protected_resource"])
                if self.context.auth_server_url is None and saved.get("auth_server_url"):
                    self.context.auth_server_url = saved["auth_server_url"]
            except Exception:  # noqa: BLE001 (an unreadable record: the next 401 discovers it again)
                log.warning("stored OAuth server metadata is unreadable; ignoring it")

        def _save_metadata(self) -> None:
            ctx = self.context
            record: dict[str, Any] = {}
            if ctx.oauth_metadata is not None:
                record["authorization_server"] = ctx.oauth_metadata.model_dump(mode="json", exclude_none=True)
            if ctx.protected_resource_metadata is not None:
                record["protected_resource"] = ctx.protected_resource_metadata.model_dump(mode="json",
                                                                                          exclude_none=True)
            if ctx.auth_server_url:
                record["auth_server_url"] = ctx.auth_server_url
            if record:
                self._store.auth.update(**{METADATA_KEY: record})

    return Provider


METADATA_KEY = "server_metadata"  # auth.json: the public OAuth metadata discovered at login (never secrets)


def _client_metadata(port: int, auth_method: str):
    from mcp.shared.auth import OAuthClientMetadata

    return OAuthClientMetadata(client_name=APP_NAME, redirect_uris=[f"http://127.0.0.1:{port}/callback"],
                               grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                               token_endpoint_auth_method=auth_method)


# ------------------------------------------------------------------------------------------ errors
def _flatten(exc: BaseException) -> list[BaseException]:
    out = [exc]
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            out.extend(_flatten(sub))
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc and len(out) < 50:
        out.extend(_flatten(cause))
    return out


def classify(exc: BaseException) -> Exception:
    """Map SDK/network failures to the engine's AuthRequired / TransportError."""
    from mcp.client.auth import OAuthFlowError
    from mcp.shared.exceptions import MCPError

    flat = _flatten(exc)
    for e in flat:
        if isinstance(e, (AuthRequired, KeyringFailure)):
            return e
    for e in flat:
        if isinstance(e, TransportError):
            return e
    for e in flat:
        if isinstance(e, OAuthFlowError):
            return AuthRequired(f"Rohlik login is no longer valid: {e}")
    for e in flat:
        if isinstance(e, MCPError):
            return TransportError(f"Rohlik MCP error {e.error.code}: {e.error.message}", retryable=True)
    for e in flat:
        if isinstance(e, (OSError, TimeoutError, ConnectionError)):
            return TransportError(f"network error: {_describe(e)}", retryable=True)
    return TransportError(f"connection failed: {_describe(flat[-1])}", retryable=True)


def _describe(e: BaseException) -> str:
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


# ------------------------------------------------------------------------------------------ transport
@dataclass
class _Pending:
    future: asyncio.Future


class McpTransport:
    def __init__(self, store: TokenStore, *, url: str = MCP_URL, interactive: bool = False,
                 port: int | None = None, auth_method: str = "client_secret_basic",
                 on_url=None, callback=None):
        self.store = store
        self.url = url
        self.interactive = interactive
        self.port = port or (store.auth.load().get("client") or {}).get("port") or 8765
        self.auth_method = auth_method
        self.on_url = on_url
        self.callback = callback
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="rohlik-mcp", daemon=True)
        self._thread.start()
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._inflight: asyncio.Future | None = None
        self.tools: list[dict] = []

    # -- public, synchronous ---------------------------------------------------------------
    def call(self, tool: str, args: dict, timeout: float) -> RawResult:
        self._ensure_session(timeout)
        fut = asyncio.run_coroutine_threadsafe(self._call(tool, args, timeout), self._loop)
        try:
            result = fut.result(timeout + 30)
        except (AuthRequired, KeyringFailure):
            self._reset_if_dead()
            raise
        except TransportError:
            self._reset_if_dead()
            raise
        except Exception as exc:  # noqa: BLE001
            self._reset_if_dead()
            raise classify(exc) from exc
        return result

    def list_tools(self) -> list[dict]:
        self._ensure_session(60)
        return self.tools

    def close(self) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(25)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(5)

    # -- internals ----------------------------------------------------------------------------
    async def _shutdown(self) -> None:
        if self._task is None:
            return
        if self._queue is not None:
            await self._queue.put(None)
        try:
            await asyncio.wait_for(self._task, 20)
        except BaseException:  # noqa: BLE001
            pass

    def _reset(self) -> None:
        self._task = None
        self._queue = None

    def _reset_if_dead(self) -> None:
        """After a failed call: reconnect next time only if the session itself ended (a JSON-RPC error answer
        leaves it usable)."""
        if self._task is not None and self._task.done():
            self._reset()

    def _ensure_session(self, timeout: float) -> None:
        if self._task is not None and not self._task.done():
            return
        ready = asyncio.run_coroutine_threadsafe(self._start(), self._loop).result(10)
        try:
            self.tools = asyncio.run_coroutine_threadsafe(self._await(ready), self._loop).result(
                LOGIN_TIMEOUT_S + 60 if self.interactive else timeout + 60)
        except (AuthRequired, KeyringFailure):
            self._reset()
            raise
        except TransportError:
            self._reset()
            raise
        except Exception as exc:  # noqa: BLE001
            self._reset()
            raise classify(exc) from exc

    async def _await(self, fut):
        return await fut

    async def _start(self) -> asyncio.Future:
        ready = self._loop.create_future()
        self._queue = asyncio.Queue()
        self._task = self._loop.create_task(self._session_main(ready, self._queue))
        return ready

    def _redirect_handler(self):
        async def handler(url: str) -> None:
            if not self.interactive:
                raise AuthRequired("no valid Rohlik session; run `login`")
            if self.on_url is not None:
                self.on_url(url)
        return handler

    async def _callback_handler(self):
        if self.callback is None:
            raise AuthRequired("no valid Rohlik session; run `login`")
        return await self.callback()

    async def _session_main(self, ready: asyncio.Future, queue: asyncio.Queue) -> None:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        provider = _provider_class()(self.url, _client_metadata(self.port, self.auth_method), self.store,
                                     redirect_handler=self._redirect_handler(),
                                     callback_handler=self._callback_handler, store=self.store)
        try:
            async with httpx2.AsyncClient(auth=provider, timeout=httpx2.Timeout(30.0, read=60.0)) as http:
                async with Client(streamable_http_client(self.url, http_client=http), read_timeout_seconds=30.0,
                                  client_info=None) as client:
                    listing = await client.list_tools()
                    tools = [{"name": t.name, "input_schema": t.input_schema} for t in listing.tools]
                    if not ready.done():
                        ready.set_result(tools)
                    while True:
                        item = await queue.get()
                        if item is None:
                            break
                        tool, args, timeout, fut = item
                        self._inflight = fut
                        try:
                            res = await client.call_tool(tool, args, read_timeout_seconds=timeout)
                            texts = [c.text for c in res.content if getattr(c, "type", None) == "text"]
                            fut.set_result(RawResult(res.structured_content, texts, bool(res.is_error)))
                        except Exception as exc:  # noqa: BLE001
                            if not fut.done():
                                fut.set_exception(classify(exc))
                        # Not in a `finally`: when the transport dies, anyio cancels call_tool, and the handler
                        # below must still find the in-flight call to fail it.
                        self._inflight = None
        except BaseException as exc:  # noqa: BLE001 (exception groups from the transport)
            err = classify(exc)
            if not ready.done():
                ready.set_exception(err)
            waiting = [self._inflight] if self._inflight is not None else []
            while not queue.empty():
                item = queue.get_nowait()
                if item is not None:
                    waiting.append(item[3])
            for fut in waiting:
                if not fut.done():
                    fut.set_exception(err)
            self._inflight = None
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise

    async def _call(self, tool: str, args: dict, timeout: float) -> RawResult:
        # Runs on the loop thread, like the session task: nothing can end the session between this check and
        # the put, and a session that ends later fails whatever is still queued.
        if self._queue is None or self._task is None or self._task.done():
            raise TransportError("the MCP session ended", retryable=True)
        fut = self._loop.create_future()
        await self._queue.put((tool, args, timeout, fut))
        return await asyncio.wait_for(fut, timeout + 20)


# ------------------------------------------------------------------------------------------ login
def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LoginTimeout(TransportError):
    def __init__(self, message: str):
        super().__init__(message, retryable=False)


class _CallbackServer:
    def __init__(self, loop: asyncio.AbstractEventLoop, port: int):
        self.loop = loop
        self.port = port
        self.result: asyncio.Future | None = None
        self.server = None

    async def start(self) -> None:
        self.result = self.loop.create_future()
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            target = line.split(" ")[1] if " " in line else "/"
            parsed = urlparse(target)
            if parsed.path != "/callback":
                body, status = b"Not found", "404 Not Found"
            else:
                q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if "error" in q:
                    body = f"Login failed: {q.get('error_description') or q['error']}".encode()
                    status = "400 Bad Request"
                    if not self.result.done():
                        self.result.set_exception(RuntimeError(f"login refused: {q['error']}"))
                else:
                    body = "Rohlik login complete — you can close this tab.".encode()
                    status = "200 OK"
                    if not self.result.done():
                        print("Browser login received; finishing...", file=sys.stderr, flush=True)
                        self.result.set_result(q)
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: "
                         f"{len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            writer.close()

    async def wait(self):
        from mcp.shared.auth import AuthorizationCodeResult

        try:
            q = await asyncio.wait_for(asyncio.shield(self.result), LOGIN_TIMEOUT_S)
        except TimeoutError:
            raise LoginTimeout("no browser login arrived within 5 minutes") from None
        return AuthorizationCodeResult(code=q.get("code", ""), state=q.get("state"), iss=q.get("iss"))

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


def login(store: TokenStore, url: str = MCP_URL) -> dict:
    """Interactive login: confidential client, authorization code + PKCE, fixed persisted loopback redirect.

    Returns facts for the envelope (V2): auth method, whether a refresh token was issued, token lifetime."""
    auth = store.auth.load()
    client = auth.get("client") or {}
    port = client.get("port")
    if not port or not _port_free(port):
        if client:
            store.clear_client()  # re-register with a new loopback port
        port = _free_port()
    store.clear_tokens()
    last_error: Exception | None = None
    for method in ("client_secret_basic", "client_secret_post"):
        def show(u: str) -> None:
            print("Log in to Rohlik in the browser tab that just opened (or open this URL yourself). "
                  "When it says 'Rohlik login complete' you are done. Waiting up to 5 minutes.\n" + u,
                  file=sys.stderr, flush=True)
            try:
                webbrowser.open(u)
            except Exception:  # noqa: BLE001
                pass
        transport = McpTransport(store, url=url, interactive=True, port=port, auth_method=method, on_url=show)
        server = _CallbackServer(transport._loop, port)
        asyncio.run_coroutine_threadsafe(server.start(), transport._loop).result(10)
        transport.callback = server.wait
        try:
            tools = transport.list_tools()
            registered = (store.auth.load().get("client") or {}).get("token_endpoint_auth_method", method)
            names = {t["name"] for t in tools}
            from .constants import ALLOWED_TOOLS
            return {"auth_method": registered, "requested_auth_method": method, "port": port,
                    "tool_count": len(names), "missing_allowlisted_tools": sorted(ALLOWED_TOOLS - names),
                    "refresh_token_issued": bool(store.refresh_token()),
                    "access_token_lifetime_s": store.auth.load().get("access_token_lifetime_s"),
                    "granted_scope": store.auth.load().get("granted_scope")}
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if "registration" not in str(exc).lower() and "token_endpoint_auth_method" not in str(exc).lower():
                break
            store.clear_client()
        finally:
            try:
                asyncio.run_coroutine_threadsafe(server.stop(), transport._loop).result(5)
            except Exception:  # noqa: BLE001
                pass
            transport.close()
    raise last_error if last_error else RuntimeError("login failed")
