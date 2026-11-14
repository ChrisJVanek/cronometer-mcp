"""OAuth 2.1 for the HTTP transport, so ChatGPT and claude.ai can connect.

Both apps add a remote MCP server by URL and then run the standard MCP
authorization flow against it: discover the metadata, register themselves
(dynamic client registration), send the user to /authorize, and swap the code
for a token. This module is the authorization server for that flow, built on
the MCP SDK's handlers.

By default it has a single user: whoever knows MCP_AUTH_PASSWORD, and every
tool runs against the account in CRONOMETER_USERNAME. With MCP_SECRET_KEY set
it has many (accounts.py): the page asks for a Cronometer email and password,
checks them against Cronometer, and the tokens it issues carry that person's
subject, so their tools run against their own account.

The consent step is a small page at /login. Clients, codes and
tokens are kept in a JSON file so a restart does not disconnect every chat.

A fixed MCP_API_KEY is also accepted as a bearer token, for clients such as
Claude Code that can send a header and need no browser flow.
"""

import hmac
import html
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path

import anyio
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

SCOPE = "cronometer"
ACCESS_TTL = 24 * 3600
REFRESH_TTL = 90 * 24 * 3600
CODE_TTL = 300
PENDING_TTL = 1800

_DEFAULT_STATE_PATH = (
    Path(os.getenv("CRONOMETER_DATA_DIR") or Path.home() / ".local" / "share" / "cronometer-mcp")
    / "oauth.json"
)


class PasswordOAuthProvider:
    """OAuthAuthorizationServerProvider with a password consent page."""

    def __init__(
        self,
        password: str,
        issuer: str,
        *,
        api_key: str | None = None,
        state_path: Path | None = None,
        local_url: str = "http://localhost:8430",
        accounts=None,
        verify: Callable[[str, str], str] | None = None,
    ) -> None:
        # accounts (an AccountStore) and verify turn on multi-user mode: verify
        # logs in to Cronometer with an email and password and returns that
        # person's subject, raising on a wrong password.
        self._accounts = accounts
        self._verify = verify
        if accounts is None and len(password) < 8:
            raise ValueError("MCP_AUTH_PASSWORD must be at least 8 characters")
        self._password = password
        self._api_key = api_key or None
        self.issuer = issuer.rstrip("/")
        self.local_url = local_url.rstrip("/")
        self._path = state_path or _DEFAULT_STATE_PATH
        self._lock = threading.Lock()
        self._failures: list[float] = []
        # Multi-user mode counts failed sign-ins per email, so one person
        # guessing cannot lock everyone else out of the page.
        self._email_failures: dict[str, list[float]] = {}
        self._state = self._load()
        if not self._path.exists() and self._state["clients"]:
            self._save()  # persist an import at once, before the variable goes

    # -- persistence ---------------------------------------------------

    def _load(self) -> dict:
        try:
            state = json.loads(self._path.read_text())
        except (FileNotFoundError, ValueError):
            state = {}
            # Moving hosts: MCP_OAUTH_IMPORT carries another server's state file
            # (base64 JSON), so connected apps keep working without signing in
            # again. Only read when there is no state yet; unset it afterwards.
            imported = os.getenv("MCP_OAUTH_IMPORT")
            if imported:
                import base64

                state = json.loads(base64.b64decode(imported))
                logger.info("Imported OAuth state for %d clients", len(state.get("clients", {})))
        for key in ("clients", "codes", "access", "refresh", "pending"):
            state.setdefault(key, {})
        return state

    def _save(self) -> None:
        now = time.time()
        for key in ("codes", "access", "refresh", "pending"):
            self._state[key] = {
                k: v
                for k, v in self._state[key].items()
                if not v.get("expires_at") or v["expires_at"] > now
            }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state))
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)

    # -- clients -------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self._state["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with self._lock:
            self._state["clients"][client_info.client_id] = client_info.model_dump(
                mode="json", exclude_none=True
            )
            self._save()
        logger.info("Registered OAuth client %s (%s)", client_info.client_id, client_info.client_name)

    # -- authorize -----------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request_id = secrets.token_urlsafe(24)
        with self._lock:
            self._state["pending"][request_id] = {
                "client_id": client.client_id,
                "client_name": client.client_name,
                "params": params.model_dump(mode="json"),
                "expires_at": time.time() + PENDING_TTL,
            }
            self._save()
        return f"{self.issuer}/login?request={request_id}"

    def _email_locked(self, email: str) -> bool:
        now = time.time()
        recent = [t for t in self._email_failures.get(email, []) if now - t < 900]
        self._email_failures[email] = recent
        if len(self._email_failures) > 10_000:  # keep memory bounded
            self._email_failures = {k: v for k, v in self._email_failures.items() if v}
        return len(recent) >= 5

    def _email_failed(self, email: str) -> None:
        self._email_failures.setdefault(email, []).append(time.time())

    def _too_many_failures(self) -> bool:
        now = time.time()
        self._failures = [t for t in self._failures if now - t < 900]
        return len(self._failures) >= 10

    async def login_page(self, request: Request) -> Response:
        """GET shows the password form; POST checks it and finishes /authorize."""
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            pending = self._state["pending"].get(request_id)
            if not pending or pending["expires_at"] < time.time():
                return HTMLResponse(_page("This sign-in link has expired. Start again from the app."), 400)
            if self._accounts is not None:
                return HTMLResponse(_account_form(request_id, pending))
            return HTMLResponse(_form(request_id, pending, self.local_url))

        form = await request.form()
        if self._accounts is not None:
            return await self._account_login(form)
        request_id = str(form.get("request", ""))
        password = str(form.get("password", ""))
        with self._lock:
            pending = self._state["pending"].get(request_id)
            if not pending or pending["expires_at"] < time.time():
                return HTMLResponse(_page("This sign-in link has expired. Start again from the app."), 400)
            if self._too_many_failures():
                return HTMLResponse(_page("Too many wrong passwords. Wait 15 minutes."), 429)
            if not hmac.compare_digest(password.encode(), self._password.encode()):
                self._failures.append(time.time())
                return HTMLResponse(_form(request_id, pending, self.local_url, error=True), 401)
            target = self._grant(request_id)
        return RedirectResponse(target, status_code=302)

    async def _account_login(self, form) -> Response:
        """Multi-user consent: sign in with the person's own Cronometer account."""
        from . import accounts as acct

        request_id = str(form.get("request", ""))
        email = str(form.get("email", "")).strip()
        password = str(form.get("password", ""))
        pending = self._state["pending"].get(request_id)
        if not pending or pending["expires_at"] < time.time():
            return HTMLResponse(_page("This sign-in link has expired. Start again from the app."), 400)

        def again(message: str, status: int) -> Response:
            return HTMLResponse(_account_form(request_id, pending, email=email, error=message), status)

        key = email.lower()
        if self._email_locked(key):
            return HTMLResponse(_page("Too many failed sign-ins for this email. Wait 15 minutes."), 429)
        if not acct.is_allowed(email):
            logger.info("Refused a sign-in from an email not on MCP_ALLOWED_EMAILS")
            return again("This email isn't on this server's list. Ask whoever runs it to add you.", 403)

        if email.lower() == acct.owner_email():
            owner_password = os.getenv("CRONOMETER_PASSWORD", "")
            if not owner_password or not hmac.compare_digest(password.encode(), owner_password.encode()):
                self._email_failed(key)
                return again("Wrong email or password.", 401)
            subject = acct.OWNER
        else:
            from .client import TwoFactorRequired

            try:
                subject = await anyio.to_thread.run_sync(self._verify, email, password)
            except TwoFactorRequired:
                return again(
                    "This Cronometer account has two-factor login on, and the server can't "
                    "sign in to it on its own. Turn two-factor off in Cronometer's settings, "
                    "then try again.",
                    400,
                )
            except _WrongPassword:
                self._email_failed(key)
                return again("Wrong email or password.", 401)
            except Exception:
                logger.exception("Cronometer sign-in check failed")
                return again("Couldn't reach Cronometer just now. Try again in a minute.", 502)
            self._accounts.put(subject, email, password)

        with self._lock:
            if request_id not in self._state["pending"]:
                return HTMLResponse(_page("This sign-in link has expired. Start again from the app."), 400)
            target = self._grant(request_id, subject)
        logger.info("Signed in %s", subject)
        return RedirectResponse(target, status_code=302)

    def _is_local(self, request: Request) -> bool:
        """True only for a browser on this machine talking to the loopback port.

        Tunnel traffic also reaches the server from 127.0.0.1, so the address
        proves nothing on its own. It does keep its public Host header and the
        Cf-* headers cloudflared adds; a request with neither, addressed to
        localhost, cannot have come through the tunnel.
        """
        host = (request.headers.get("host") or "").split(":")[0]
        via_tunnel = any(h.lower().startswith("cf-") for h in request.headers)
        return host in ("localhost", "127.0.0.1") and not via_tunnel

    async def approve_page(self, request: Request) -> Response:
        """Consent without the password, for the owner sitting at this Mac.

        Served only on http://localhost:<port>, which nothing off this machine
        can reach. POST also requires a same-origin form, so another site cannot
        submit it from the owner's browser.
        """
        if not self._is_local(request):
            return HTMLResponse(_page("Approval without a password only works on this computer."), 403)
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            pending = self._state["pending"].get(request_id)
            if not pending or pending["expires_at"] < time.time():
                return HTMLResponse(_page("This sign-in link has expired. Start again from the app."), 400)
            return HTMLResponse(_approve_form(request_id, pending))

        origin = request.headers.get("origin")
        site = request.headers.get("sec-fetch-site")
        if (origin and origin.rstrip("/") != self.local_url) or (site and site != "same-origin"):
            return HTMLResponse(_page("Refused: this approval must come from the page itself."), 403)
        form = await request.form()
        request_id = str(form.get("request", ""))
        with self._lock:
            pending = self._state["pending"].get(request_id)
            if not pending or pending["expires_at"] < time.time():
                return HTMLResponse(_page("This sign-in link has expired. Start again from the app."), 400)
            target = self._grant(request_id)
        return RedirectResponse(target, status_code=302)

    def _grant(self, request_id: str, subject: str = "owner") -> str:
        """Turn a pending /authorize into a code; returns the client redirect.
        Caller holds the lock."""
        pending = self._state["pending"].pop(request_id)
        params = AuthorizationParams.model_validate(pending["params"])
        code = secrets.token_urlsafe(32)
        self._state["codes"][code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [SCOPE],
            expires_at=time.time() + CODE_TTL,
            client_id=pending["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=subject,
        ).model_dump(mode="json")
        self._save()
        logger.info("Authorized OAuth client %s", pending["client_id"])
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    # -- codes and tokens ----------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        raw = self._state["codes"].get(authorization_code)
        if not raw or raw["client_id"] != client.client_id:
            return None
        return AuthorizationCode.model_validate(raw)

    def _issue(
        self, client_id: str, scopes: list[str], resource: str | None, subject: str | None
    ) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = int(time.time())
        common = {
            "client_id": client_id,
            "scopes": scopes,
            "resource": resource,
            "subject": subject or "owner",
        }
        self._state["access"][access] = {"token": access, "expires_at": now + ACCESS_TTL, **common}
        self._state["refresh"][refresh] = {"token": refresh, "expires_at": now + REFRESH_TTL, **common}
        self._save()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with self._lock:
            if self._state["codes"].pop(authorization_code.code, None) is None:
                raise TokenError("invalid_grant", "authorization code already used")
            return self._issue(
                client.client_id,
                authorization_code.scopes,
                authorization_code.resource,
                authorization_code.subject,
            )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        raw = self._state["refresh"].get(refresh_token)
        if not raw or raw["client_id"] != client.client_id or raw["expires_at"] < time.time():
            return None
        return RefreshToken.model_validate(raw)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        with self._lock:
            # Rotate: a refresh token works once.
            if self._state["refresh"].pop(refresh_token.token, None) is None:
                raise TokenError("invalid_grant", "refresh token already used")
            return self._issue(
                client.client_id,
                scopes or refresh_token.scopes,
                refresh_token.resource,
                refresh_token.subject,
            )

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self._api_key and hmac.compare_digest(token.encode(), self._api_key.encode()):
            return AccessToken(token=token, client_id="api-key", scopes=[SCOPE], subject="owner")
        raw = self._state["access"].get(token)
        if not raw or raw["expires_at"] < time.time():
            return None
        return AccessToken.model_validate(raw)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._lock:
            self._state["access"].pop(token.token, None)
            self._state["refresh"].pop(token.token, None)
            self._save()


def _page(body: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cronometer MCP</title>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;background:#f6f6f4;color:#222;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;padding:16px}}
main{{background:#fff;border-radius:12px;padding:28px;max-width:360px;width:100%;
box-shadow:0 2px 12px rgba(0,0,0,.08)}}
h1{{font-size:20px;margin:0 0 8px}} p{{color:#555;line-height:1.4}}
input{{width:100%;box-sizing:border-box;padding:10px;font-size:16px;border:1px solid #ccc;border-radius:8px}}
button{{margin-top:12px;width:100%;padding:11px;font-size:16px;border:0;border-radius:8px;
background:#ff763f;color:#fff;cursor:pointer}} .err{{color:#b00020}}
@media (prefers-color-scheme: dark){{body{{background:#111;color:#eee}}
main{{background:#1d1d1d}} p{{color:#aaa}} input{{background:#111;color:#eee;border-color:#444}}}}
</style></head><body><main>{body}</main></body></html>"""


def _redirect_host(pending: dict) -> str:
    from urllib.parse import urlparse

    return urlparse(str((pending.get("params") or {}).get("redirect_uri", ""))).netloc


def _form(request_id: str, pending: dict, local_url: str, *, error: bool = False) -> str:
    client_name = pending.get("client_name") or "An app"
    message = '<p class="err">Wrong password.</p>' if error else ""
    rid = html.escape(request_id)
    return _page(
        f"""<h1>Connect Cronometer</h1>
<p><b>{html.escape(client_name)}</b> ({html.escape(_redirect_host(pending))}) wants to read and
change your Cronometer diary.</p>
{message}
<form method="post" action="/login">
<input type="hidden" name="request" value="{rid}">
<input type="password" name="password" placeholder="Server password" autofocus required>
<button type="submit">Allow</button>
</form>
<p style="font-size:14px">On the computer running this server?
<a href="{html.escape(local_url)}/approve?request={rid}">Approve there without a password</a>.</p>"""
    )


def _account_form(request_id: str, pending: dict, *, email: str = "", error: str = "") -> str:
    client_name = pending.get("client_name") or "An app"
    message = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return _page(
        f"""<h1>Connect Cronometer</h1>
<p><b>{html.escape(client_name)}</b> ({html.escape(_redirect_host(pending))}) wants to read and
change your Cronometer diary. Sign in with your Cronometer account.</p>
{message}
<form method="post" action="/login">
<input type="hidden" name="request" value="{html.escape(request_id)}">
<input type="email" name="email" placeholder="Cronometer email" value="{html.escape(email)}"
 autocomplete="username" autofocus required>
<input type="password" name="password" placeholder="Cronometer password"
 autocomplete="current-password" required style="margin-top:8px">
<button type="submit">Allow</button>
</form>
<p style="font-size:14px">The server keeps your Cronometer password, encrypted, so it can stay
signed in for you.</p>"""
    )


def _approve_form(request_id: str, pending: dict) -> str:
    client_name = pending.get("client_name") or "An app"
    return _page(
        f"""<h1>Connect Cronometer</h1>
<p><b>{html.escape(client_name)}</b> wants to read and change your Cronometer diary.
After you allow it, you go back to <b>{html.escape(_redirect_host(pending))}</b>.</p>
<form method="post" action="/approve">
<input type="hidden" name="request" value="{html.escape(request_id)}">
<button type="submit">Allow on this computer</button>
</form>"""
    )


class _WrongPassword(Exception):
    """verify() raises this when Cronometer rejects the email or password."""


WrongPassword = _WrongPassword

__all__ = ["SCOPE", "AuthorizeError", "PasswordOAuthProvider", "WrongPassword"]
