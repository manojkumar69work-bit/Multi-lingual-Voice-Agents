from __future__ import annotations
"""Password hashing for tenant and admin credentials.

Tenant passwords used to sit in tenants.json in plaintext. That file is read by
two processes, backed up, and edited by hand — so a single leaked copy handed
over every client portal. This module hashes them with PBKDF2-HMAC-SHA256 from
the standard library, keeping the project's zero-new-dependency rule.

Stored format (single self-describing string, like Django's):

    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>

Legacy plaintext values are still accepted by `verify` so nobody is locked out
mid-upgrade; `is_hashed` lets the caller re-store the hashed form on next
successful login. See auth.authenticate for that migration.
"""

import hashlib
import hmac
import os

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 260_000
SALT_BYTES = 16


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    """Return the encoded hash for a plaintext password."""
    if not password:
        return ""
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def is_hashed(stored: str) -> bool:
    """True when the stored value is already in encoded form (not plaintext)."""
    return bool(stored) and stored.startswith(ALGORITHM + "$")


def verify(password: str, stored: str) -> bool:
    """Constant-time check of a plaintext password against a stored value.

    Accepts both encoded hashes and legacy plaintext, so an existing
    tenants.json keeps working until each password is upgraded on next login.
    """
    if not stored or password is None:
        return False

    if not is_hashed(stored):
        # Legacy plaintext row.
        return hmac.compare_digest(password, stored)

    try:
        _, iter_s, salt_hex, hash_hex = stored.split("$", 3)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iter_s)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def ensure_hashed(password: str) -> str:
    """Idempotently return an encoded hash — pass through values already hashed.

    Used on the admin write path so a password typed into the portal is never
    persisted in the clear, while re-saving an unchanged client is a no-op.
    """
    if not password or is_hashed(password):
        return password
    return hash_password(password)
