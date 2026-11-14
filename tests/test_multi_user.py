"""Several people on one hosted server, each with their own Cronometer account.

Cronometer's login is faked: the owner is user 1, "mum" is user 42, and
"twofa" has two-factor login on. Everything else (the connect page, the
encrypted store, the tokens' subject, which account a tool runs against, and
paged results staying with their owner) runs for real.
"""

from __future__ import annotations

import importlib
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient
from test_oauth import MCP_HEADERS, PUBLIC, REDIRECT, _pkce

OWNER = ("owner@example.com", "owner-password")
MUM = ("Mum@Example.com", "mums-password")
TWOFA = ("twofa@example.com", "whatever")
IDS = {"owner@example.com": 1, "mum@example.com": 42}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_AUTH_PASSWORD", raising=False)
    monkeypatch.setenv("MCP_SECRET_KEY", "a-long-test-secret-key")
    monkeypatch.setenv("MCP_ALLOWED_EMAILS", "mum@example.com, twofa@example.com")
    monkeypatch.setenv("CRONOMETER_USERNAME", OWNER[0])
    monkeypatch.setenv("CRONOMETER_PASSWORD", OWNER[1])
    monkeypatch.setenv("CRONOMETER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("cronometer_mcp.auth._DEFAULT_STATE_PATH", tmp_path / "oauth.json")

    from cronometer_mcp import client as client_module
    from cronometer_mcp.client import CronometerError, TwoFactorRequired

    logins = []

    def fake_login(self):
        email, password = self._get_credentials()
        logins.append(email)
        if email.lower() == TWOFA[0]:
            raise TwoFactorRequired("2FA")
        expected = {OWNER[0]: OWNER[1], MUM[0].lower(): MUM[1]}
        if expected.get(email.lower()) != password:
            raise CronometerError("Login failed: {'result': 'FAIL'}")
        self._user_id = IDS[email.lower()]
        self._token = f"token-{self._user_id}"
        self._timezone = "Australia/Brisbane"
        self._save_cached_session()

    monkeypatch.setattr(client_module.CronometerClient, "login", fake_login)
    monkeypatch.setattr(
        client_module.CronometerClient, "get_profile", lambda self: {"timezone": "Australia/Brisbane"}
    )

    from cronometer_mcp import server as server_module

    server_module = importlib.reload(server_module)
    server_module._enable_remote(PUBLIC, "127.0.0.1", 8430)
    server_module.logins = logins
    server_module.tmp = tmp_path
    return server_module


@pytest.fixture
def http(server):
    with TestClient(server.mcp.streamable_http_app(), base_url=PUBLIC) as c:
        yield c


def _start(http):
    reg = http.post(
        "/register",
        json={
            "redirect_uris": [REDIRECT],
            "client_name": "ChatGPT",
            "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    ).json()
    verifier, challenge = _pkce()
    r = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s",
            "scope": "cronometer",
        },
        follow_redirects=False,
    )
    rid = parse_qs(urlparse(r.headers["location"]).query)["request"][0]
    return reg, verifier, rid


def _sign_in(http, email, password):
    reg, verifier, rid = _start(http)
    page = http.get(f"/login?request={rid}")
    assert 'name="email"' in page.text
    r = http.post(
        "/login", data={"request": rid, "email": email, "password": password}, follow_redirects=False
    )
    if r.status_code != 302:
        return r, None
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tok = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
            "client_id": reg["client_id"],
            "client_secret": reg["client_secret"],
        },
    )
    assert tok.status_code == 200, tok.text
    return r, {**tok.json(), **reg}


def _call(http, access, tool, arguments=None):
    headers = {**MCP_HEADERS, "authorization": f"Bearer {access}"}
    init = http.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
    )
    headers["mcp-session-id"] = init.headers["mcp-session-id"]
    http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = http.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        },
    )
    text = re.search(r'"text":"((?:[^"\\]|\\.)*)"', r.text).group(1)
    return json.loads(json.loads(f'"{text}"'))


def _subject(server, access):
    return server.mcp._auth_server_provider._state["access"][access]["subject"]


def test_each_person_reaches_their_own_account(server, http):
    _, mum = _sign_in(http, *MUM)
    _, owner = _sign_in(http, *OWNER)
    assert _subject(server, mum["access_token"]) == "cm-42"
    assert _subject(server, owner["access_token"]) == "owner"

    assert _call(http, mum["access_token"], "connection_status")["user_id"] == 42
    assert _call(http, owner["access_token"], "connection_status")["user_id"] == 1
    # Mum's first tool call reused the sign-in's login instead of a second one.
    assert server.logins.count(MUM[0]) == 1


def test_password_is_stored_encrypted(server, http):
    _sign_in(http, *MUM)
    raw = (server.tmp / "accounts.json").read_text()
    assert "cm-42" in raw
    assert MUM[1] not in raw and MUM[0] not in raw
    assert server._accounts.get("cm-42") == (MUM[0], MUM[1])


def test_refresh_keeps_the_person(server, http):
    _, mum = _sign_in(http, *MUM)
    r = http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": mum["refresh_token"],
            "client_id": mum["client_id"],
            "client_secret": mum["client_secret"],
        },
    )
    assert _subject(server, r.json()["access_token"]) == "cm-42"


def test_refusals(server, http):
    wrong, _ = _sign_in(http, MUM[0], "nope")
    assert wrong.status_code == 401 and "Wrong email or password" in wrong.text

    stranger, _ = _sign_in(http, "stranger@example.com", "anything")
    assert stranger.status_code == 403 and "isn&#x27;t on this server" in stranger.text
    assert "stranger@example.com" not in server.logins  # never sent to Cronometer

    twofa, _ = _sign_in(http, *TWOFA)
    assert twofa.status_code == 400 and "two-factor" in twofa.text

    owner_wrong, _ = _sign_in(http, OWNER[0], "nope")
    assert owner_wrong.status_code == 401
    assert server._accounts.subjects() == []


def test_anyone_when_star(server, http, monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_EMAILS", "*")
    r, _ = _sign_in(http, "stranger@example.com", "anything")
    assert r.status_code == 401  # allowed through, then Cronometer said no
    assert "stranger@example.com" in server.logins


def test_paged_results_stay_with_their_owner(server, monkeypatch):
    from cronometer_mcp import paging

    who = {"subject": "cm-42"}
    monkeypatch.setattr(paging, "owner", lambda: who["subject"])
    first = json.loads(paging.fit({"items": [{"n": "x" * 500} for _ in range(400)]}))
    result_id = first["paging"]["result_id"]
    assert json.loads(paging.get_result_page(result_id, offset=10))["paging"]["offset"] == 10
    who["subject"] = "owner"
    assert json.loads(paging.get_result_page(result_id))["status"] == "error"
