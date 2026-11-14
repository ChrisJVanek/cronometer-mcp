"""The people who use one hosted server, each with their own Cronometer account.

When MCP_SECRET_KEY is set, the connect page asks for a Cronometer email and
password instead of the shared server password. The server checks them by
logging in to Cronometer, then keeps them so it can sign in again when the
Cronometer session expires. They are stored encrypted with MCP_SECRET_KEY
(Fernet: AES-128-CBC with an HMAC), in accounts.json next to the OAuth state.

Each person is a subject: "cm-<Cronometer user id>". OAuth tokens carry it, and
every tool call runs against that person's account. The owner (the account in
CRONOMETER_USERNAME) keeps the subject "owner", so apps connected before
multi-user mode keep working.

Who may connect: the owner always, plus the emails in MCP_ALLOWED_EMAILS
(comma separated, case ignored), or anyone with a Cronometer account when it
is "*".
"""

import base64
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

OWNER = "owner"


def data_dir() -> Path:
    return Path(os.getenv("CRONOMETER_DATA_DIR") or Path.home() / ".local" / "share" / "cronometer-mcp")


def _fernet(secret: str) -> Fernet:
    """A Fernet key from any passphrase (or an actual Fernet key)."""
    try:
        return Fernet(secret.encode())
    except ValueError:
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def allowed_emails() -> set[str] | None:
    """The emails allowed to connect; None means anyone."""
    raw = os.getenv("MCP_ALLOWED_EMAILS", "")
    emails = {e.strip().lower() for e in raw.split(",") if e.strip()}
    return None if "*" in emails else emails


def owner_email() -> str:
    return os.getenv("CRONOMETER_USERNAME", "").strip().lower()


def is_allowed(email: str) -> bool:
    email = email.strip().lower()
    if email and email == owner_email():
        return True
    allowed = allowed_emails()
    return allowed is None or email in allowed


class AccountStore:
    """Encrypted Cronometer credentials, by subject."""

    def __init__(self, secret: str, path: Path | None = None) -> None:
        if len(secret) < 16:
            raise ValueError("MCP_SECRET_KEY must be at least 16 characters")
        self._fernet = _fernet(secret)
        self._path = path or data_dir() / "accounts.json"
        self._lock = threading.Lock()
        try:
            self._accounts: dict = json.loads(self._path.read_text())
        except (FileNotFoundError, ValueError):
            self._accounts = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._accounts))
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)

    def put(self, subject: str, email: str, password: str) -> None:
        sealed = self._fernet.encrypt(json.dumps({"email": email, "password": password}).encode())
        with self._lock:
            self._accounts[subject] = {
                "sealed": sealed.decode(),
                "updated_at": int(time.time()),
            }
            self._save()

    def get(self, subject: str) -> tuple[str, str] | None:
        raw = self._accounts.get(subject)
        if not raw:
            return None
        try:
            data = json.loads(self._fernet.decrypt(raw["sealed"].encode()))
        except (InvalidToken, ValueError, KeyError):
            logger.error("Could not decrypt the account %s: has MCP_SECRET_KEY changed?", subject)
            return None
        return data["email"], data["password"]

    def remove(self, subject: str) -> None:
        with self._lock:
            if self._accounts.pop(subject, None) is not None:
                self._save()

    def subjects(self) -> list[str]:
        return list(self._accounts)


def subject_dir(subject: str) -> Path:
    """Where one person's Cronometer sessions are cached."""
    safe = "".join(c for c in subject if c.isalnum() or c in "-_")
    return data_dir() / "users" / safe
