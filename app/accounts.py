"""Creator accounts for the hosted link page.

Three things here need care, because this is the first part of the system
that holds other people's property:

1. Passwords are stored as scrypt hashes with a per-user salt. Never
   reversible, never logged.
2. A creator's OGAds API key is *their* credential. It is encrypted at rest
   with Fernet so a leaked database backup does not hand out live API keys,
   and it is decrypted only in the request that needs it.
3. Login is rate limited per username and per IP, because an unthrottled
   login form on a public signup site is a credential-stuffing target.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import store
from .config import BASE_DIR, settings, set_env_value

log = logging.getLogger("ogads.accounts")

USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{1,28}[a-z0-9])$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")
SESSION_MAX_AGE = 60 * 60 * 24 * 30

# Usernames that would collide with our own routes or impersonate the site.
RESERVED = {
    "admin", "api", "static", "login", "logout", "signup", "dashboard", "u",
    "go", "lp", "review", "postback", "healthz", "settings", "support",
    "help", "about", "terms", "privacy", "root", "system", "official",
}

_login_attempts: dict[str, list[float]] = {}
MAX_ATTEMPTS, ATTEMPT_WINDOW = 8, 900


# ------------------------------------------------------------------ secrets
def _app_secret() -> str:
    """Signing secret for session cookies, generated once and persisted."""
    secret = os.getenv("APP_SECRET", "").strip()
    if not secret:
        secret = secrets.token_urlsafe(48)
        set_env_value("APP_SECRET", secret)
        log.info("generated APP_SECRET and wrote it to .env")
    return secret


def _fernet() -> Fernet:
    key = os.getenv("CREDENTIAL_KEY", "").strip()
    if not key:
        key = Fernet.generate_key().decode()
        set_env_value("CREDENTIAL_KEY", key)
        log.info("generated CREDENTIAL_KEY and wrote it to .env")
    return Fernet(key.encode())


def encrypt_key(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_key(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError):
        # Usually means CREDENTIAL_KEY was rotated or lost. Loud, because
        # every affected creator's page silently stops earning.
        log.error("could not decrypt a stored API key -- CREDENTIAL_KEY changed?")
        return ""


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    got = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(got, expected)


def password_problem(password: str) -> str:
    if len(password) < 10:
        return "Use at least 10 characters."
    if password.lower() in {"password12", "1234567890", "qwertyuiop"}:
        return "That password is too common."
    return ""


# ----------------------------------------------------------------- sessions
def issue_session(creator_id: int) -> str:
    return URLSafeTimedSerializer(_app_secret(), salt="creator-session").dumps(creator_id)


def read_session(token: str) -> int | None:
    if not token:
        return None
    try:
        return URLSafeTimedSerializer(_app_secret(), salt="creator-session").loads(
            token, max_age=SESSION_MAX_AGE)
    except (BadSignature, ValueError):
        return None


def csrf_token(session_token: str) -> str:
    return hmac.new(_app_secret().encode(), session_token.encode(), hashlib.sha256).hexdigest()


def csrf_ok(session_token: str, supplied: str) -> bool:
    return bool(supplied) and hmac.compare_digest(csrf_token(session_token), supplied)


# ------------------------------------------------------------- rate limiting
def too_many_attempts(identifier: str) -> bool:
    now = time.monotonic()
    attempts = [t for t in _login_attempts.get(identifier, []) if now - t < ATTEMPT_WINDOW]
    _login_attempts[identifier] = attempts
    return len(attempts) >= MAX_ATTEMPTS


def record_attempt(identifier: str) -> None:
    _login_attempts.setdefault(identifier, []).append(time.monotonic())


def clear_attempts(identifier: str) -> None:
    _login_attempts.pop(identifier, None)


# -------------------------------------------------------------- validation
def username_problem(username: str) -> str:
    u = (username or "").strip().lower()
    if not USERNAME_RE.match(u):
        return ("Usernames are 3-30 characters: lowercase letters, digits, "
                "hyphen or underscore, starting and ending with a letter or digit.")
    if u in RESERVED:
        return "That username is reserved."
    if store.get_creator_by_username(u):
        return "That username is taken."
    return ""


def email_problem(email: str) -> str:
    e = (email or "").strip().lower()
    if not EMAIL_RE.match(e):
        return "That does not look like an email address."
    if store.get_creator_by_email(e):
        return "There is already an account with that email."
    return ""
