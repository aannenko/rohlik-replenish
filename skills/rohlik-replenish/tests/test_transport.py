"""McpTransport session handling with the SDK's `Client` replaced by a scripted one: a JSON-RPC error answer keeps
the session, and a transport failure (which anyio surfaces as a cancelled call plus an exception group when the
session closes) fails the call at once and reconnects on the next call."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from engine.mcp_transport import McpTransport, MemoryKeyring, SecretStore, TokenStore
from engine.paths import AuthFile
from engine.rohlik import TransportError


def scripted_client():
    from mcp import types
    from mcp.shared.exceptions import MCPError

    sessions = []

    class Client:
        def __init__(self, transport, read_timeout_seconds=None, client_info=None):
            sessions.append(self)
            self.number = len(sessions)
            self.dead = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            if self.dead:
                raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionResetError("connection reset")])
            return False

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="get_cart", input_schema={"type": "object"})])

        async def call_tool(self, tool, args, read_timeout_seconds=None):
            if tool == "rpc_error":
                raise MCPError(types.INTERNAL_ERROR, "Server returned an error response")
            if tool == "die":
                self.dead = True
                raise asyncio.CancelledError()
            text = json.dumps({"tool": tool, "session": self.number})
            return SimpleNamespace(content=[types.TextContent(text=text)], structured_content=None, is_error=False)

    return Client, sessions


@pytest.fixture
def transport(tmp_path, monkeypatch):
    import mcp
    import mcp.client.streamable_http as streamable_http

    client, sessions = scripted_client()
    monkeypatch.setattr(mcp, "Client", client)
    monkeypatch.setattr(streamable_http, "streamable_http_client", lambda url, http_client=None: None)
    t = McpTransport(TokenStore(SecretStore(backend=MemoryKeyring()), AuthFile(tmp_path)))
    yield t, sessions
    t.close()


def session_of(result) -> int:
    return json.loads(result.texts[0])["session"]


def test_error_answer_keeps_the_session(transport):
    t, sessions = transport
    assert [x["name"] for x in t.list_tools()] == ["get_cart"]
    assert session_of(t.call("get_cart", {}, 30)) == 1
    with pytest.raises(TransportError, match="Server returned an error response") as err:
        t.call("rpc_error", {}, 30)
    assert err.value.retryable
    assert session_of(t.call("get_cart", {}, 30)) == 1 and len(sessions) == 1


def test_dead_session_fails_the_call_at_once_and_reconnects(transport):
    t, sessions = transport
    assert session_of(t.call("get_cart", {}, 30)) == 1
    started = time.monotonic()
    with pytest.raises(TransportError, match="connection reset") as err:
        t.call("die", {}, 30)
    assert time.monotonic() - started < 5, "the in-flight call must fail when the session ends, not time out"
    assert err.value.retryable
    assert session_of(t.call("get_cart", {}, 30)) == 2 and len(sessions) == 2
