"""Minimal cookie-session auth for the admin + client portals.

Design goals: zero new dependencies (uses PyJWT, already required), no DB tables,
and complete isolation between clients. A signed JWT is stored in an HttpOnly
cookie. Two roles:

  - admin  → full access; credentials from env (ADMIN_USER / ADMIN_PASSWORD)
  - client → scoped to its own client_id; credentials = tenant.id + tenant.password

FastAPI dependencies `require_admin` and `require_client` read and verify the
cookie. `require_client` returns the caller's client_id so handlers can scope
every query and never leak another agency's data.
"""
from __future__ import annotations

import hmac
import logging
import os
import time

import jwt
from fastapi import Cookie, HTTPException, Response

import passwords
import tenants

logger = logging.getLogger("auth")

# ── Config ───────────────────────────────────────────────────────────────────
# A dev default keeps local setup zero-config; OVERRIDE in production via .env.
DEV_SESSION_SECRET = "dev-insecure-change-me"
DEV_ADMIN_PASSWORD = "admin123"

SESSION_SECRET = os.environ.get("SESSION_SECRET", DEV_SESSION_SECRET)
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
# Prefer a PBKDF2 hash so the admin password need not sit in .env in the clear.
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", DEV_ADMIN_PASSWORD)

COOKIE_NAME = "va_session"
SESSION_TTL = int(os.environ.get("SESSION_TTL_SECONDS", str(7 * 24 * 3600)))
# Cookies are sent over http://localhost during dev, so secure=False by default.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"

# Set APP_ENV=production once this is exposed to the internet.
IS_PRODUCTION = os.environ.get("APP_ENV", "development").lower() == "production"


def check_config() -> list[str]:
    """Report insecure settings. Fatal in production, warnings in development.

    Called at server startup so an accidental public deploy fails loudly rather
    than silently shipping a well-known session secret and admin/admin123.
    """
    problems = []
    if SESSION_SECRET == DEV_SESSION_SECRET:
        problems.append("SESSION_SECRET is the built-in dev default — anyone can forge a session cookie")
    if not ADMIN_PASSWORD_HASH and ADMIN_PASSWORD == DEV_ADMIN_PASSWORD:
        problems.append("ADMIN_PASSWORD is the built-in dev default (admin123)")
    if not COOKIE_SECURE:
        problems.append("COOKIE_SECURE=0 — session cookies will be sent over plain HTTP")

    for p in problems:
        (logger.error if IS_PRODUCTION else logger.warning)("INSECURE CONFIG: %s", p)
    if problems and IS_PRODUCTION:
        raise RuntimeError(
            "Refusing to start in production with insecure auth config: "
            + "; ".join(problems)
        )
    return problems


# ── Token helpers ────────────────────────────────────────────────────────────

def _issue_token(role: str, subject: str) -> str:
    now = int(time.time())
    payload = {"role": role, "sub": subject, "iat": now, "exp": now + SESSION_TTL}
    return jwt.encode(payload, SESSION_SECRET, algorithm="HS256")


def _decode(token: str) -> dict | None:
    try:
        return jwt.decode(token, SESSION_SECRET, algorithms=["HS256"])
    except Exception:
        return None


def set_session_cookie(response: Response, role: str, subject: str) -> None:
    token = _issue_token(role, subject)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


# ── Login ────────────────────────────────────────────────────────────────────

def authenticate(username: str, password: str) -> dict | None:
    """Validate credentials. Returns {"role", "client_id", "name"} or None.

    Admin is checked first. Then clients by tenant id + password. Comparisons
    are constant-time so neither half leaks through timing.
    """
    username = (username or "").strip()
    password = password or ""

    admin_stored = ADMIN_PASSWORD_HASH or ADMIN_PASSWORD
    if hmac.compare_digest(username, ADMIN_USER) and passwords.verify(password, admin_stored):
        return {"role": "admin", "client_id": "", "name": "Administrator"}

    t = tenants.get_tenant(username)
    if t and t.id != "default" and t.active and t.password:
        if passwords.verify(password, t.password):
            # Upgrade legacy plaintext rows the first time their owner logs in,
            # so tenants.json converges to hashes without a migration script.
            if not passwords.is_hashed(t.password):
                try:
                    tenants.upsert_tenant({"id": t.id, "password": password})
                    logger.info("Upgraded stored password for client %r to PBKDF2", t.id)
                except Exception as e:
                    logger.warning("Password upgrade failed for %r: %s", t.id, e)
            return {"role": "client", "client_id": t.id, "name": t.name or t.id}

    return None


# ── FastAPI dependencies ─────────────────────────────────────────────────────

def _session(token: str | None) -> dict:
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = _decode(token)
    if not data:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return data


def require_admin(va_session: str | None = Cookie(default=None)) -> dict:
    data = _session(va_session)
    if data.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return data


def require_client(va_session: str | None = Cookie(default=None)) -> str:
    """Return the authenticated client's client_id (scopes all client queries)."""
    data = _session(va_session)
    if data.get("role") != "client" or not data.get("sub"):
        raise HTTPException(status_code=403, detail="Client access required")
    return data["sub"]


def current_user(va_session: str | None = Cookie(default=None)) -> dict | None:
    """Soft lookup for /api/auth/me — never raises."""
    if not va_session:
        return None
    data = _decode(va_session)
    if not data:
        return None
    role = data.get("role")
    sub = data.get("sub", "")
    name = "Administrator"
    if role == "client":
        t = tenants.get_tenant(sub)
        name = (t.name if t else "") or sub
    return {"role": role, "client_id": sub if role == "client" else "", "name": name}
