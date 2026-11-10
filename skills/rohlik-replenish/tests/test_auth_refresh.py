"""§13.1 Auth through the real SDK adapter.

`replenish.py login` and `run` execute for real: the CLI, `McpTransport`, the OAuth provider subclass, `TokenStore`,
the SDK's OAuth flow, `mcp.Client` and its streamable-HTTP transport, and the loopback redirect server. Only the
network, the browser and the OS keyring are replaced:
- the network is an in-memory Rohlik laid out like the real one (public metadata read 2026-10-26): the MCP endpoint
  at mcp.rohlik.cz names the authorization server https://identity.rohlik.cz, whose token endpoint is
  /oauth2/token, while https://mcp.rohlik.cz/token answers 401. Its tools serve the synthetic household
  (tests/synthetic.py);
- the browser stub "logs in" and follows the redirect to the loopback callback;
- the keyring lives in memory.
Each command builds a new transport and provider, so only the keyring and auth.json carry over, as between two
processes."""

import base64
import hashlib
import json
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from engine import cli
from engine.fake import TOOLS, FakeRohlik
from engine.mcp_transport import MemoryKeyring, SecretStore
from engine.paths import DATA_DIR_ENV, AuthFile

MCP = "https://mcp.rohlik.cz/mcp"
PRM = "https://mcp.rohlik.cz/.well-known/oauth-protected-resource/mcp"
AS = "https://identity.rohlik.cz"
TOKEN = f"{AS}/oauth2/token"
WRONG_TOKEN_URL = "https://mcp.rohlik.cz/token"  # where the SDK sends a refresh when it knows no metadata


class Rohlik:
    def __init__(self, fixtures: Path):
        self.fake = FakeRohlik.from_dir(fixtures)
        self.requests: list[dict] = []
        self.codes: dict[str, str] = {}  # authorization code -> PKCE challenge
        self.access: set[str] = set()
        self.refresh: set[str] = set()
        self.reject_refresh = False
        self.rotate = True  # False: a refresh answer carries no refresh token, and the old one stays valid
        self.issued = 0
        self.browser_opened: list[str] = []

    # the browser: the user logs in, and Rohlik redirects to the loopback callback
    def browser(self, url: str, *args, **kwargs) -> bool:
        self.browser_opened.append(url)
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert url.startswith(f"{AS}/oauth2/authorize?") and q["code_challenge_method"] == "S256", url
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = q["code_challenge"]
        target = f"{q['redirect_uri']}?code={code}&state={q['state']}"
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=10).read(), daemon=True).start()
        return True

    # the network
    def handle(self, request):
        import httpx2

        url = str(request.url)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()} if url == TOKEN else {}
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        entry = {"method": request.method, "url": url, "form": form, "bearer": bearer, "rpc": None}
        self.requests.append(entry)
        if url == PRM:
            return httpx2.Response(200, json={"resource": MCP, "authorization_servers": [AS],
                                              "scopes_supported": ["openid", "email", "roles"],
                                              "bearer_methods_supported": ["header"]})
        if url in (f"{AS}/.well-known/oauth-authorization-server", f"{AS}/.well-known/openid-configuration"):
            return httpx2.Response(200, json={
                "issuer": AS, "authorization_endpoint": f"{AS}/oauth2/authorize", "token_endpoint": TOKEN,
                "registration_endpoint": f"{AS}/connect/register", "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"]})
        if url == f"{AS}/connect/register":
            # like Rohlik (V2): a confidential client, registered as client_secret_post whatever was asked
            return httpx2.Response(201, json={**json.loads(request.content), "client_id": "client-1",
                                              "client_secret": "secret-1",
                                              "token_endpoint_auth_method": "client_secret_post"})
        if url == TOKEN:
            return self._token(form)
        if url == MCP:
            return self._mcp(request, entry)
        return httpx2.Response(401 if url == WRONG_TOKEN_URL else 404)

    def _token(self, form: dict):
        import httpx2

        if form.get("client_id") != "client-1" or form.get("client_secret") != "secret-1":
            return httpx2.Response(401, json={"error": "invalid_client"})
        if form["grant_type"] == "authorization_code":
            challenge = self.codes.pop(form.get("code", ""), None)
            digest = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
            if challenge is None or digest.rstrip(b"=").decode() != challenge:
                return httpx2.Response(400, json={"error": "invalid_grant"})
        elif form["grant_type"] == "refresh_token":
            if self.reject_refresh or form.get("refresh_token") not in self.refresh:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            if not self.rotate:
                self.issued += 1
                access = f"access-{self.issued}"
                self.access.add(access)
                return httpx2.Response(200, json={"access_token": access, "token_type": "Bearer",
                                                  "expires_in": 28799, "scope": "openid roles email"})
            self.refresh.discard(form["refresh_token"])  # rotation: each refresh token works once
        else:
            return httpx2.Response(400, json={"error": "unsupported_grant_type"})
        self.issued += 1
        access, refresh = f"access-{self.issued}", f"refresh-{self.issued}"
        self.access.add(access)
        self.refresh.add(refresh)
        return httpx2.Response(200, json={"access_token": access, "token_type": "Bearer", "expires_in": 28799,
                                          "refresh_token": refresh, "scope": "openid roles email"})

    def _mcp(self, request, entry: dict):
        import httpx2

        if entry["bearer"] not in self.access:
            return httpx2.Response(401, headers={
                "WWW-Authenticate": f'Bearer error="invalid_token", resource_metadata="{PRM}"'})
        if request.method != "POST":
            return httpx2.Response(405)
        msg = json.loads(request.content)
        entry["rpc"] = msg.get("method")
        if "id" not in msg:
            return httpx2.Response(202)
        if msg["method"] == "initialize":
            result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "rohlik-in-memory", "version": "1"}}
        elif msg["method"] == "tools/list":
            result = {"tools": [{"name": t, "inputSchema": {"type": "object"}} for t in TOOLS]}
        elif msg["method"] == "tools/call":
            raw = self.fake.call(msg["params"]["name"], msg["params"].get("arguments") or {}, 30)
            result = {"content": [{"type": "text", "text": t} for t in raw.texts], "isError": raw.is_error}
            if raw.structured is not None:
                result["structuredContent"] = raw.structured
        else:  # server/discover: a handshake-era server
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": msg["id"],
                                              "error": {"code": -32601, "message": "Method not found"}})
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": result})

    # views
    def grants(self, since: int = 0) -> list[str]:
        return [r["form"].get("grant_type") for r in self.requests[since:] if r["url"] == TOKEN]

    def tool_calls(self, since: int = 0) -> list[dict]:
        return [r for r in self.requests[since:] if r["rpc"] == "tools/call"]


@pytest.fixture
def rohlik(tmp_path, monkeypatch, synthetic_fixtures):
    import webbrowser

    import httpx2
    import keyring

    net = Rohlik(synthetic_fixtures)
    real = httpx2.AsyncClient

    class InMemoryClient(real):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx2.MockTransport(net.handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", InMemoryClient)
    net.keyring = MemoryKeyring()
    monkeypatch.setattr(keyring, "get_password", net.keyring.get_password)
    monkeypatch.setattr(keyring, "set_password", net.keyring.set_password)
    monkeypatch.setattr(keyring, "delete_password", net.keyring.delete_password)
    monkeypatch.setattr(webbrowser, "open", net.browser)
    net.data_dir = tmp_path / "data"
    monkeypatch.setenv(DATA_DIR_ENV, str(net.data_dir))
    return net


def command(capsys, *argv) -> dict:
    cli.main(list(argv))
    return json.loads(capsys.readouterr().out)  # exactly one envelope


def log_in_then_expire(rohlik, capsys) -> None:
    env = command(capsys, "login")
    assert env["status"] == "ok" and env["data"]["refresh_token_issued"] is True, env
    assert env["data"]["auth_method"] == "client_secret_post"
    assert env["data"]["tool_count"] == len(TOOLS) and "tools" not in env["data"], "a count, not the names"
    assert env["data"]["missing_allowlisted_tools"] == []
    assert len(rohlik.browser_opened) == 1 and rohlik.grants() == ["authorization_code"]
    # nine hours later: the 8-hour access token has expired, the refresh token is still valid
    secrets = SecretStore(backend=rohlik.keyring)
    access = json.loads(secrets.get("access_token"))
    access["expires_at"] = time.time() - 3600
    secrets.set("access_token", json.dumps(access))


def test_a_new_process_refreshes_an_expired_access_token_silently(rohlik, capsys):
    log_in_then_expire(rohlik, capsys)
    saved = AuthFile(rohlik.data_dir).load()
    assert saved["server_metadata"]["authorization_server"]["token_endpoint"] == TOKEN
    assert "secret-1" not in (rohlik.data_dir / "auth.json").read_text(), "never secrets in auth.json"
    mark = len(rohlik.requests)
    env = command(capsys, "run", "--dry-run")
    # a new user: the run reads the account, orders and cart, then asks for tags (or finds too little history)
    assert env["status"] in ("needs_tags", "insufficient_history"), env
    assert rohlik.grants(mark) == ["refresh_token"], "one refresh, before the first request"
    assert all(r["url"] != WRONG_TOKEN_URL for r in rohlik.requests)
    calls = rohlik.tool_calls(mark)
    assert calls and {r["bearer"] for r in calls} == {"access-2"}
    assert len(rohlik.browser_opened) == 1, "no browser"
    secrets = SecretStore(backend=rohlik.keyring)
    assert secrets.get("refresh_token") == "refresh-2", "the rotated refresh token is stored"
    access = json.loads(secrets.get("access_token"))
    assert access["token"] == "access-2" and access["expires_at"] > time.time() + 8 * 3600 - 120
    # and the next process uses the new access token without refreshing again
    mark = len(rohlik.requests)
    env = command(capsys, "run", "--dry-run")
    assert env["status"] in ("needs_tags", "insufficient_history") and rohlik.grants(mark) == []


def test_without_the_stored_metadata_the_sdk_refreshes_at_the_wrong_url(rohlik, capsys):
    """Why the provider stores the server metadata: without it, the SDK sends the refresh to `<MCP origin>/token`,
    which isn't Rohlik's token endpoint, so the session is lost after 8 hours."""
    log_in_then_expire(rohlik, capsys)
    AuthFile(rohlik.data_dir).update(server_metadata=None)
    mark = len(rohlik.requests)
    env = command(capsys, "run", "--dry-run")
    assert env["status"] == "auth_required", env
    assert [r["url"] for r in rohlik.requests[mark:] if r["method"] == "POST"][0] == WRONG_TOKEN_URL
    assert rohlik.grants(mark) == [] and not rohlik.tool_calls(mark)


def test_a_refresh_answer_without_a_refresh_token_keeps_the_stored_one(rohlik, capsys):
    """§4.2: only a rejected refresh ends the session. A server that doesn't rotate answers a refresh without a
    refresh token; the stored one stays, and the next process refreshes with it again."""
    log_in_then_expire(rohlik, capsys)
    rohlik.rotate = False
    mark = len(rohlik.requests)
    env = command(capsys, "run", "--dry-run")
    assert env["status"] in ("needs_tags", "insufficient_history"), env
    assert rohlik.grants(mark) == ["refresh_token"]
    secrets = SecretStore(backend=rohlik.keyring)
    assert secrets.get("refresh_token") == "refresh-1", "kept"
    assert AuthFile(rohlik.data_dir).load()["refresh_token_issued"] is True
    access = json.loads(secrets.get("access_token"))
    access["expires_at"] = time.time() - 3600  # another 8 hours later
    secrets.set("access_token", json.dumps(access))
    mark = len(rohlik.requests)
    env = command(capsys, "run", "--dry-run")
    assert env["status"] in ("needs_tags", "insufficient_history"), env
    assert rohlik.grants(mark) == ["refresh_token"] and len(rohlik.browser_opened) == 1


def test_token_store_keeps_the_refresh_token_when_an_answer_has_none(tmp_path):
    """The same rule at the storage boundary, whatever the SDK passes on."""
    import asyncio

    from mcp.shared.auth import OAuthToken

    from engine.mcp_transport import TokenStore

    store = TokenStore(SecretStore(backend=MemoryKeyring()), AuthFile(tmp_path), clock=lambda: 1_000_000.0)
    asyncio.run(store.set_tokens(OAuthToken(access_token="a1", token_type="Bearer", expires_in=100,
                                            refresh_token="r1")))
    asyncio.run(store.set_tokens(OAuthToken(access_token="a2", token_type="Bearer", expires_in=100)))
    assert store.refresh_token() == "r1" and store.access()["token"] == "a2"
    assert AuthFile(tmp_path).load()["refresh_token_issued"] is True
    store.clear_tokens()  # a new login starts from nothing, so a login without a refresh token stores none
    asyncio.run(store.set_tokens(OAuthToken(access_token="a3", token_type="Bearer", expires_in=100)))
    assert store.refresh_token() is None and AuthFile(tmp_path).load()["refresh_token_issued"] is False


def test_a_rejected_refresh_asks_for_login_without_opening_a_browser(rohlik, capsys):
    log_in_then_expire(rohlik, capsys)
    rohlik.reject_refresh = True
    mark = len(rohlik.requests)
    env = command(capsys, "run")
    assert env["status"] == "auth_required" and env["data"] == {"wrote": False}, env
    assert "Run `login`" in env["summary_md"]
    assert rohlik.grants(mark) == ["refresh_token"]
    assert len(rohlik.browser_opened) == 1, "a run never opens a browser"
    assert not rohlik.tool_calls(mark)


def test_a_keyring_that_cannot_be_read_gives_an_error_envelope(rohlik, capsys, monkeypatch):
    import keyring
    import keyring.errors

    log_in_then_expire(rohlik, capsys)

    def locked(service, name):
        raise keyring.errors.KeyringLocked("the keyring is locked")
    monkeypatch.setattr(keyring, "get_password", locked)
    mark = len(rohlik.requests)
    env = command(capsys, "run")
    assert env["status"] == "error" and env["data"]["stage"] == "auth" and env["data"]["wrote"] is False, env
    assert env["data"]["actions"] and "keyring" in env["summary_md"] and "KeyringLocked" in env["summary_md"]
    assert len(rohlik.requests) == mark, "nothing was sent"


def test_a_keyring_that_cannot_store_the_refreshed_tokens_gives_an_error_envelope(rohlik, capsys, monkeypatch):
    """The failure happens inside the SDK's refresh, when the provider stores the new tokens."""
    import keyring
    import keyring.errors

    log_in_then_expire(rohlik, capsys)

    def refused(service, name, value):
        raise keyring.errors.PasswordSetError("Credential Manager refused the write")
    monkeypatch.setattr(keyring, "set_password", refused)
    mark = len(rohlik.requests)
    env = command(capsys, "run")
    assert env["status"] == "error" and env["data"]["stage"] == "auth" and env["data"]["wrote"] is False, env
    assert "couldn't be written" in env["summary_md"] and env["data"]["actions"]
    assert rohlik.grants(mark) == ["refresh_token"] and not rohlik.tool_calls(mark)
