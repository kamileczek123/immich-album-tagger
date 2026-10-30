"""Login for the web UI.

Credentials live in ``CONFIG_DIR/credentials.yaml``::

    encrypted: false
    username: alex
    password: my secret

On start-up, a file with ``encrypted: false`` is rewritten with both values replaced by
one-way scrypt hashes and ``encrypted: true``. From then on a login is checked by hashing
what was typed with the stored salt; the plain values are not kept anywhere. To change the
login, write plain values again with ``encrypted: false``.

Values may reference environment variables (``${UI_PASS_HASH}``), like the other config
files. With hashes made once by :func:`hash_secret` and kept in a secret store, the file can
be deployed with ``encrypted: true`` on every deploy and never holds a plain password::

    encrypted: true
    username: ${UI_USER_HASH}
    password: ${UI_PASS_HASH}

Make a hash with::

    python -c "from immich_album_tagger.auth import hash_secret; print(hash_secret('secret'))"

Hashes look like ``scrypt:<salt>:<digest>`` (no ``$``, so they pass through ``.env`` files
and Docker Compose unchanged). The older ``scrypt$<salt>$<digest>`` form is still accepted.

If the file cannot be rewritten (read-only mount), the app still works with the plain
values in memory and logs a warning.

Sessions are signed cookies (HMAC-SHA256) with a key stored in DATA_DIR/session.key, so they
survive restarts; deleting that file logs everyone out.

With ``auth.mode: immich_keys`` (:class:`KeySessions`) the cookie carries a session id
instead, and every login gets its own Immich API key, created with the person's login and
deleted again at logout or expiry. Those keys live only in memory: after a restart the
cookie no longer matches a session and the person logs in again; keys left in Immich by the
restart are deleted at that next login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .config import expand_env
from .immich import ImmichError

log = logging.getLogger(__name__)

COOKIE = "iat_session"
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def _digest(value: str, salt: bytes) -> str:
    raw = hashlib.scrypt(value.encode(), salt=salt, dklen=32, **_SCRYPT)
    return base64.b64encode(raw).decode()


def hash_secret(value: str, salt: bytes | None = None) -> str:
    """One-way hash ``scrypt:<salt>:<digest>`` (base64 never contains ':')."""
    salt = salt or secrets.token_bytes(16)
    return f"scrypt:{base64.b64encode(salt).decode()}:{_digest(value, salt)}"


def verify_secret(value: str, stored: str) -> bool:
    """Check ``value`` against a hash in the current (':') or the 0.4.0 ('$') format."""
    try:
        separator = ":" if stored.startswith("scrypt:") else "$"
        scheme, salt, digest = stored.split(separator)
        if scheme != "scrypt":
            return False
        return hmac.compare_digest(_digest(value, base64.b64decode(salt)), digest)
    except (ValueError, TypeError):
        return False


@dataclass
class Credentials:
    username: str
    password: str
    encrypted: bool

    def check(self, username: str, password: str) -> bool:
        if self.encrypted:
            # Evaluate both, so timing does not reveal which one was wrong.
            user_ok = verify_secret(username, self.username)
            pass_ok = verify_secret(password, self.password)
            return user_ok and pass_ok
        user_ok = hmac.compare_digest(username.encode(), self.username.encode())
        pass_ok = hmac.compare_digest(password.encode(), self.password.encode())
        return user_ok and pass_ok


class AuthError(RuntimeError):
    pass


def load_credentials(path: Path) -> Credentials | None:
    """Read credentials.yaml; hash it in place if it is still plain. None = no login."""
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise AuthError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise AuthError(f"{path}: top level must be a mapping")
    data = expand_env(data)
    username = str(data.get("username") or "")
    password = str(data.get("password") or "")
    encrypted = bool(data.get("encrypted", False))
    if not username or not password:
        raise AuthError(
            f"{path}: username and password are required "
            "(if they come from ${...}, check that the environment variables are set)"
        )
    creds = Credentials(username, password, encrypted)
    if not encrypted:
        hashed = {
            "encrypted": True,
            "username": hash_secret(username),
            "password": hash_secret(password),
        }
        header = (
            "# Hashed by immich-album-tagger. To change the login, replace both values with\n"
            "# plain text and set encrypted: false; they are hashed again on the next start.\n"
        )
        try:
            path.write_text(header + yaml.safe_dump(hashed, sort_keys=False), encoding="utf-8")
            os.chmod(path, 0o600)
            log.info("credentials.yaml was plain text; it is now hashed")
            creds = Credentials(hashed["username"], hashed["password"], True)
        except OSError as exc:
            log.warning(
                "Cannot rewrite %s (%s); login works, but the file stays in plain text. "
                "Mount the config directory read-write to hash it.",
                path,
                exc,
            )
    return creds


class Sessions:
    def __init__(self, key_path: Path, hours: float):
        self.max_age = int(hours * 3600)
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.exists():
            self._key = key_path.read_bytes()
        else:
            self._key = secrets.token_bytes(32)
            key_path.write_bytes(self._key)
            os.chmod(key_path, 0o600)

    def issue(self, user: str = "") -> str:
        """Signed token ``<expires>.<user, base64>.<signature>``."""
        expires = str(int(time.time()) + self.max_age)
        who = base64.urlsafe_b64encode(user.encode()).decode().rstrip("=") or "-"
        payload = f"{expires}.{who}"
        return f"{payload}.{self._sign(payload)}"

    def user(self, token: str | None) -> str | None:
        """The user of a valid token ("" if none was recorded), or None if invalid/expired."""
        if not token or token.count(".") != 2:
            return None
        expires, who, signature = token.split(".")
        if not hmac.compare_digest(signature, self._sign(f"{expires}.{who}")):
            return None
        if not (expires.isdigit() and int(expires) > time.time()):
            return None
        if who == "-":
            return ""
        try:
            return base64.urlsafe_b64decode(who + "=" * (-len(who) % 4)).decode()
        except ValueError:
            return None

    def valid(self, token: str | None) -> bool:
        return self.user(token) is not None

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()


class ImmichLogin:
    """Log in with an Immich account (``auth.mode: immich``).

    The e-mail and password are checked by Immich itself (``POST /auth/login``); the session
    Immich opens is closed again at once (``POST /auth/logout``). Only accounts in
    ``allowed`` may log in; with an empty list only the owner of the API key may.
    """

    def __init__(self, immich, allowed: list[str]):
        self.immich = immich
        self.allowed = {value.strip().casefold() for value in allowed if value.strip()}

    def check(self, email: str, password: str) -> tuple[str, dict | None]:
        """Returns (result, user): result is "ok", "invalid", "not_allowed" or "unreachable"."""
        try:
            user = self.immich.verify_login(email, password)
        except Exception as exc:  # noqa: BLE001 - network or server error
            log.warning("Immich login check failed: %s", exc)
            return "unreachable", None
        if user is None:
            return "invalid", None
        if self.allowed:
            ok = {user["id"].casefold(), user["email"].casefold()} & self.allowed
        else:
            ok = user["id"] == self.immich.me()["id"]
        return ("ok" if ok else "not_allowed"), user


# --------------------------------------------------------------------------- immich_keys

KEY_NAME_PREFIX = "immich-album-tagger session"
# Only keys named exactly like this are treated as leftovers of this app and deleted.
_KEY_NAME = re.compile(
    r"^" + re.escape(KEY_NAME_PREFIX) + r" \d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z [0-9a-f]{8}$"
)


def key_name(expires_at: float) -> str:
    """``immich-album-tagger session <expiry, UTC> <8 random hex>``: unique, recognisable."""
    expiry = datetime.fromtimestamp(expires_at, UTC).strftime("%Y-%m-%dT%H:%MZ")
    return f"{KEY_NAME_PREFIX} {expiry} {secrets.token_hex(4)}"


def is_session_key(name: str) -> bool:
    return bool(_KEY_NAME.match(name or ""))


@dataclass
class KeySession:
    id: str
    user_id: str
    email: str
    name: str
    key_id: str
    key_secret: str = field(repr=False)
    expires_at: float = 0.0
    client: Any = field(default=None, repr=False)  # ImmichClient acting with the key
    service: Any = field(default=None, repr=False)  # set by the app in on_open


class KeySessions:
    """Logins with an Immich account, each acting with its own temporary API key.

    ``open`` logs in to Immich, deletes keys left over from earlier sessions of that person
    (named by :func:`key_name`, not in memory), creates a key with ``permissions`` plus
    ``apiKey.delete`` and closes the Immich session again. ``close`` and ``sweep`` (expiry)
    delete the key with the key itself; if Immich cannot be reached, the deletion is retried
    by the next ``sweep``. Secrets are never written to disk.
    """

    def __init__(
        self,
        immich,
        permissions: list[str],
        hours: float,
        allowed: list[str] | None = None,
        on_open: Callable[[KeySession], None] | None = None,
        on_close: Callable[[KeySession], None] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.immich = immich  # used anonymously and with session tokens
        self.permissions = list(dict.fromkeys([*permissions, "apiKey.delete"]))
        self.max_age = int(hours * 3600)
        self.allowed = {value.strip().casefold() for value in allowed or [] if value.strip()}
        self._on_open = on_open
        self._on_close = on_close
        self._clock = clock
        self._lock = threading.Lock()
        self._live: dict[str, KeySession] = {}
        self._dead: dict[str, KeySession] = {}  # closed, key not deleted yet
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ sessions

    def open(self, email: str, password: str) -> tuple[str, KeySession | None, dict | None]:
        """Returns (result, session, user); result is "ok", "invalid", "not_allowed",
        "unreachable" or "key_failed"."""
        try:
            user = self.immich.login(email, password)
        except Exception as exc:  # noqa: BLE001 - network or server error
            log.warning("Immich login failed: %s", exc)
            return "unreachable", None, None
        if user is None:
            return "invalid", None, None
        token = user.pop("token")
        try:
            names = {user["id"].casefold(), user["email"].casefold()}
            if self.allowed and not names & self.allowed:
                return "not_allowed", None, user
            self._delete_leftovers(token, user)
            expires_at = self._clock() + self.max_age
            try:
                key = self.immich.create_api_key(token, key_name(expires_at), self.permissions)
            except ImmichError as exc:
                log.error("Cannot create an API key for %s: %s", user["email"], exc)
                return "key_failed", None, user
        finally:
            self.immich.logout(token)
        session = KeySession(
            id=secrets.token_urlsafe(24),
            user_id=user["id"],
            email=user["email"],
            name=user["name"],
            key_id=key["id"],
            key_secret=key["secret"],
            expires_at=expires_at,
        )
        session.client = self.immich.with_key(key["secret"])
        if self._on_open:
            try:
                self._on_open(session)
            except Exception:  # noqa: BLE001 - do not leave the key behind
                log.exception("Could not start the session of %s", user["email"])
                self._finish(session)
                return "key_failed", None, user
        with self._lock:
            self._live[session.id] = session
        log.info("API key %s created for %s", session.key_id, session.email)
        return "ok", session, user

    def get(self, session_id: str | None) -> KeySession | None:
        """The live session, or None (unknown, e.g. after a restart, or expired)."""
        with self._lock:
            session = self._live.get(session_id or "")
        if session is None or session.expires_at <= self._clock():
            return None
        return session

    def close(self, session_id: str | None) -> bool:
        """Log out: delete the session's key. False if there was no such session."""
        with self._lock:
            session = self._live.pop(session_id or "", None)
        if session is None:
            return False
        self._finish(session)
        return True

    def sweep(self) -> int:
        """Close expired sessions and retry failed key deletions. Returns keys deleted."""
        now = self._clock()
        with self._lock:
            expired = [s for s in self._live.values() if s.expires_at <= now]
            for session in expired:
                del self._live[session.id]
            retry = list(self._dead.values())
        deleted = sum(self._finish(session) for session in expired)
        for session in retry:
            if self._delete_key(session):
                with self._lock:
                    self._dead.pop(session.id, None)
                session.client.close()
                deleted += 1
        return deleted

    def close_all(self) -> None:
        """On shutdown: delete every key still held (best effort)."""
        with self._lock:
            sessions = list(self._live.values())
            self._live.clear()
        for session in sessions:
            self._finish(session)
        self.sweep()

    def active(self) -> list[KeySession]:
        with self._lock:
            return list(self._live.values())

    # ------------------------------------------------------------------ keys

    def _finish(self, session: KeySession) -> bool:
        """Stop what runs for the session, then delete its key. True if deleted."""
        if self._on_close:
            try:
                self._on_close(session)
            except Exception:  # noqa: BLE001 - the key must still be deleted
                log.exception("Error while closing the session of %s", session.email)
        if self._delete_key(session):
            session.client.close()
            return True
        with self._lock:
            self._dead[session.id] = session
        return False

    def _delete_key(self, session: KeySession) -> bool:
        try:
            session.client.delete_api_key(session.key_id)
        except ImmichError as exc:
            if exc.status in (400, 401, 404):  # already gone (key deleted by hand, ...)
                return True
            log.warning("Cannot delete API key %s of %s: %s", session.key_id, session.email, exc)
            return False
        log.info("API key %s of %s deleted", session.key_id, session.email)
        return True

    def _delete_leftovers(self, token: str, user: dict) -> None:
        """Delete keys of this person left by earlier sessions (restart, crash)."""
        try:
            keys = self.immich.list_api_keys(token)
        except ImmichError as exc:
            log.warning("Cannot list API keys of %s: %s", user["email"], exc)
            return
        with self._lock:
            held = {s.key_id for s in self._live.values()}
        for key in keys:
            if key["id"] in held or not is_session_key(key.get("name", "")):
                continue
            try:
                self.immich.delete_api_key(key["id"], token=token)
                log.info("Deleted leftover API key %r of %s", key.get("name"), user["email"])
            except ImmichError as exc:
                log.warning("Cannot delete leftover API key %s: %s", key["id"], exc)

    # ------------------------------------------------------------------ background

    def start(self, interval_minutes: float) -> None:
        def loop() -> None:
            while not self._stop.wait(interval_minutes * 60):
                try:
                    self.sweep()
                except Exception:  # noqa: BLE001 - keep sweeping
                    log.exception("Key sweep failed")

        if self._thread is None:
            self._thread = threading.Thread(target=loop, name="key-sweep", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
