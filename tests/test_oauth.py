"""The OAuth flow ChatGPT and claude.ai run against the HTTP transport.

Walks the whole thing the way a connector does: discovery, dynamic client
registration, /authorize, the password page, the code exchange, a refresh,
and finally an MCP tools/list with the bearer token. No Cronometer calls.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import re
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

PUBLIC = "http://localhost:8430"
PASSWORD = "correct-horse-battery"
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
MCP_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_AUTH_PASSWORD", PASSWORD)
    monkeypatch.setenv("MCP_API_KEY", "static-test-key")
    monkeypatch.setattr("cronometer_mcp.auth._DEFAULT_STATE_PATH", tmp_path / "oauth.json")
    from cronometer_mcp import server

    server = importlib.reload(server)
    server._enable_remote(PUBLIC, "127.0.0.1", 8430)
    app = server.mcp.streamable_http_app()
    with TestClient(app, base_url=PUBLIC) as c:
        yield c


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


def _authorize(client, client_id, challenge, password=PASSWORD):
    r = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "cronometer",
            "resource": f"{PUBLIC}/mcp",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    login = urlparse(r.headers["location"])
    assert login.path == "/login"
    request_id = parse_qs(login.query)["request"][0]
    page = client.get(f"/login?request={request_id}")
    assert "Connect Cronometer" in page.text
    return client.post(
        "/login", data={"request": request_id, "password": password}, follow_redirects=False
    ), request_id


def test_unauthenticated_mcp_points_at_metadata(client):
    r = client.post("/mcp", json={}, headers=MCP_HEADERS)
    assert r.status_code == 401
    assert "resource_metadata" in r.headers["www-authenticate"]
    meta = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["authorization_servers"][0].rstrip("/") == PUBLIC
    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["registration_endpoint"].endswith("/register")
    assert "S256" in asm["code_challenge_methods_supported"]


def test_full_flow_then_tools_list(client):
    reg = client.post(
        "/register",
        json={
            "redirect_uris": [REDIRECT],
            "client_name": "ChatGPT",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert reg.status_code == 201, reg.text
    creds = reg.json()

    verifier, challenge = _pkce()
    bad, _ = _authorize(client, creds["client_id"], challenge, password="nope")
    assert bad.status_code == 401

    done, _ = _authorize(client, creds["client_id"], challenge)
    assert done.status_code == 302
    back = urlparse(done.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == REDIRECT
    q = parse_qs(back.query)
    assert q["state"] == ["xyz"]

    tok = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
    )
    assert tok.status_code == 200, tok.text
    tokens = tok.json()

    # A code works once.
    again = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
    )
    assert again.status_code == 400

    refreshed = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    access = refreshed.json()["access_token"]

    headers = {**MCP_HEADERS, "authorization": f"Bearer {access}"}
    init = client.post(
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
    assert init.status_code == 200, init.text
    session = init.headers["mcp-session-id"]
    headers["mcp-session-id"] = session
    client.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    listed = client.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    names = set(re.findall(r'"name":"([a-z_]+)"', listed.text))
    assert {"search", "fetch", "get_food_log", "add_food_entry", "export_data"} <= names


def test_static_api_key(client):
    r = client.post(
        "/mcp",
        headers={**MCP_HEADERS, "authorization": "Bearer static-test-key"},
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
    assert r.status_code == 200, r.text


def _pending_request(client):
    reg = client.post(
        "/register",
        json={"redirect_uris": [REDIRECT], "client_name": "ChatGPT", "token_endpoint_auth_method": "none"},
    ).json()
    _, challenge = _pkce()
    r = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s",
        },
        follow_redirects=False,
    )
    return parse_qs(urlparse(r.headers["location"]).query)["request"][0]


def test_local_approval_without_password(client):
    rid = _pending_request(client)
    assert "Approve there without a password" in client.get(f"/login?request={rid}").text
    local = {"host": "localhost:8430"}
    assert client.get(f"/approve?request={rid}", headers=local).status_code == 200
    done = client.post(
        "/approve",
        data={"request": rid},
        headers={**local, "origin": "http://localhost:8430", "sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert done.status_code == 302
    assert "code=" in done.headers["location"]


def test_approval_refused_through_tunnel_or_cross_site(client):
    rid = _pending_request(client)
    # The public hostname, as every tunnel request carries.
    public = {"host": "cronometer.example.com"}
    r = client.post("/approve", data={"request": rid}, headers=public, follow_redirects=False)
    assert r.status_code == 403
    # Loopback host but cloudflared's headers present.
    tunneled = {"host": "localhost:8430", "cf-ray": "abc", "cf-connecting-ip": "1.2.3.4"}
    assert client.post("/approve", data={"request": rid}, headers=tunneled).status_code == 403
    # Another site auto-submitting the form in the owner's browser.
    cross = {"host": "localhost:8430", "origin": "https://evil.example", "sec-fetch-site": "cross-site"}
    assert client.post("/approve", data={"request": rid}, headers=cross).status_code == 403


def test_state_import_when_empty(tmp_path, monkeypatch):
    import base64
    import json

    from cronometer_mcp.auth import PasswordOAuthProvider

    state = {"clients": {"c1": {"client_id": "c1", "redirect_uris": [REDIRECT]}}, "access": {}}
    monkeypatch.setenv("MCP_OAUTH_IMPORT", base64.b64encode(json.dumps(state).encode()).decode())
    fresh = PasswordOAuthProvider(PASSWORD, PUBLIC, state_path=tmp_path / "oauth.json")
    assert "c1" in fresh._state["clients"]
    # An existing state file always wins over the import.
    (tmp_path / "kept.json").write_text(json.dumps({"clients": {}}))
    kept = PasswordOAuthProvider(PASSWORD, PUBLIC, state_path=tmp_path / "kept.json")
    assert kept._state["clients"] == {}
