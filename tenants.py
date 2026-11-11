from __future__ import annotations
"""Multi-tenant config: each business client is one *tenant*.

A client is identified by a stable `id` slug (e.g. "apollo-clinic"). The slug is:
  - the room-metadata key the agent uses to attribute a call  ({"tenant": "<id>"})
  - the client's login username for the client portal
  - the `client_id` written onto every call row in call_store

`phone_number` is kept as an optional field so that, once a real SIP DID is wired,
the dispatch rule can route by called-number → client. Until then it is unused.

The store is a JSON file (tenants.json). The admin portal performs CRUD through
upsert_tenant / delete_tenant, which persist to JSON and hot-reload the registry.
The default tenant is used when metadata is missing/unknown/malformed.

`mode` selects which built-in persona a tenant gets when its system_prompt is
blank: "business" (the lead-capturing assistant) or "chat" (an open conversation
partner, with no lead capture). Both exist in every supported language.
"""
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field, asdict, fields
import secrets
import time
from pathlib import Path

import call_store
import languages
import passwords

logger = logging.getLogger("tenants")

# Serializes writes so concurrent admin edits don't corrupt tenants.json.
_WRITE_LOCK = threading.Lock()


# ── Per-tenant (per-client) config ──────────────────────────────────────────
# `language` is any code in languages.py — "hi" (Hinglish, Roman-script Hindi,
# the default), "te", "en", "ta", "kn", "ml", "mr", "bn", "gu", "pa", "od".
# Unknown values fall back to the default. The TTS service picks a native voice
# for each language automatically.
#
# `mode` is one of:
#   "business" — the lead-capturing assistant for a client's business  ← default
#   "chat"     — an open conversation partner: no business scope, no lead capture.
#                The built-in prompt and greeting exist for all three languages.
MODES = ("business", "chat")


@dataclass
class TenantConfig:
    id: str = "default"                 # stable slug — primary key & login username
    name: str = ""                      # Agency name (display)
    language: str = "hi"                # a languages.py code; see above
    mode: str = "business"              # "business" | "chat"
    agent_name: str = "Riya"            # Name the AI agent uses on the call
    system_prompt: str = ""             # Custom LLM prompt; blank → built-in template
    business_info: str = ""             # Knowledge base of business facts; injected into LLM
    greeting: str = ""                  # Custom first line; blank → built-in greeting
    phone_number: str = ""              # Optional E.164 DID for SIP routing (later)
    password: str = ""                  # Client-portal login password
    whatsapp_to: str = ""               # Optional per-client lead-alert WhatsApp number
    sheet_id: str = ""                  # Per-client Google Sheet; blank → GOOGLE_SHEET_ID
    webhook_url: str = ""               # Optional per-client lead webhook (CRM/Zapier/n8n)
    webhook_secret: str = ""            # If set, leads are HMAC-signed (X-MEA-Signature)
    minute_quota: int = 0               # Monthly minute cap; 0 = unlimited
    enforce_quota: bool = False         # If true, agent refuses calls past the cap
    active: bool = True                 # Soft on/off switch for the client
    business_type: str = ""             # e.g. "real estate agency", "medical clinic", "car dealership"
    role_description: str = ""          # e.g. "a professional real estate consultant", "a polite medical receptionist"
    lead_fields: str = ""               # comma-separated field list, e.g. "name,property_type,budget,location,timeline,notes"
    closing_instructions: str = ""      # optional custom closing behavior; blank uses generic fallback
    call_key: str = ""                  # public key for the client's call page / widget (32 hex)
    session_version: int = 1            # bumped on password change or pause; client JWTs carry it
    created_at: float = 0.0
    updated_at: float = 0.0

    # Fields never sent to a browser. `password` is a PBKDF2 hash and
    # `webhook_secret` signs outbound lead payloads — neither is re-displayable,
    # and echoing them into an admin form field only risks leaking them.
    SECRET_FIELDS = ("password", "webhook_secret")

    def public_dict(self) -> dict:
        """Full config including secrets — for persistence and internal merges."""
        return asdict(self)

    def safe_dict(self) -> dict:
        """Config for API responses: secrets replaced by a set/unset flag."""
        d = asdict(self)
        for f in self.SECRET_FIELDS:
            d[f"{f}_set"] = bool(d.pop(f, ""))
        return d


# ── Defaults ───────────────────────────────────────────────────────────────
DEFAULT_TENANT = TenantConfig(
    id="default",
    name="Default",
    language="hi",
    agent_name="Riya",
    business_type="",
    role_description="",
    lead_fields="",
    closing_instructions="",
)

# ── Storage ────────────────────────────────────────────────────────────────
# Tenants live in the `tenants` table of the shared SQLite file (call_store).
# Every read goes to the table, so the server and the agent worker — separate
# processes — always see the row the admin just saved; there is no cache to
# invalidate and no restart to schedule. `tenants.json` is imported once if the
# table is empty (then renamed *.imported), and `save_tenants()` now writes an
# export copy rather than the source of truth.

_TENANTS_PATH = os.environ.get(
    "TENANTS_CONFIG",
    os.path.join(os.path.dirname(__file__), "tenants.json"),
)

# Persisted fields, in column order. `id` is the primary key.
TENANT_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(TenantConfig))


class TenantExists(ValueError):
    """Raised by upsert_tenant(create_only=True) when the id is taken."""


def slugify(text: str) -> str:
    """Turn an agency name into a stable id slug."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or "client"


def _normalize_number(num: str) -> str:
    """Normalize phone number for matching: keep digits and a leading +."""
    if not num:
        return ""
    num = num.strip().replace(" ", "").replace("-", "")
    return num


def _tenant_from_dict(d: dict, *, fallback_id: str = "") -> TenantConfig:
    """Build a TenantConfig from a raw dict, tolerating missing keys."""
    tid = (d.get("id") or fallback_id or slugify(d.get("name", ""))).strip()
    return TenantConfig(
        id=tid,
        name=d.get("name", ""),
        language=languages.normalize(d.get("language")),
        # Unknown values fall back to the business assistant rather than a
        # tenant that silently gets no scope guard.
        mode=(d.get("mode") or "business") if (d.get("mode") or "business") in MODES else "business",
        agent_name=d.get("agent_name", "Riya"),
        system_prompt=d.get("system_prompt", ""),
        business_info=d.get("business_info", ""),
        greeting=d.get("greeting", ""),
        phone_number=_normalize_number(d.get("phone_number", "")),
        password=d.get("password", ""),
        whatsapp_to=d.get("whatsapp_to", ""),
        sheet_id=d.get("sheet_id", ""),
        webhook_url=d.get("webhook_url", ""),
        webhook_secret=d.get("webhook_secret", ""),
        minute_quota=int(d.get("minute_quota", 0) or 0),
        enforce_quota=bool(d.get("enforce_quota", False)),
        active=bool(d.get("active", True)),
        business_type=d.get("business_type", ""),
        role_description=d.get("role_description", ""),
        lead_fields=d.get("lead_fields", ""),
        closing_instructions=d.get("closing_instructions", ""),
        call_key=d.get("call_key", "") or "",
        session_version=int(d.get("session_version", 1) or 1),
        created_at=float(d.get("created_at", 0) or 0),
        updated_at=float(d.get("updated_at", 0) or 0),
    )


def _row_to_cfg(row) -> TenantConfig:
    return _tenant_from_dict(dict(row))


def _default() -> TenantConfig:
    return DEFAULT_TENANT


def _conn():
    return call_store.get_conn()


def _read(tenant_id: str) -> TenantConfig | None:
    row = _conn().execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    return _row_to_cfg(row) if row else None


def _write(cfg: TenantConfig) -> None:
    d = cfg.public_dict()
    d["enforce_quota"] = 1 if d["enforce_quota"] else 0
    d["active"] = 1 if d["active"] else 0
    cols = ", ".join(TENANT_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in TENANT_COLUMNS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in TENANT_COLUMNS if c not in ("id", "created_at"))
    with _WRITE_LOCK:
        _conn().execute(
            f"INSERT INTO tenants ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            d,
        )
        _conn().commit()


def import_json_if_empty(path: str | None = None) -> int:
    """One-time import of the legacy tenants.json into the table.

    Runs only when the table has no rows and the file exists. The file is
    renamed to `<name>.imported` afterwards so it is never imported twice and
    never mistaken for the live config."""
    p = Path(path or _TENANTS_PATH)
    if os.environ.get("APP_ENV", "development").lower() == "production":
        # The seed file holds demo clients with placeholder passwords; never
        # let it create internet-reachable logins.
        return 0
    count = _conn().execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
    if count or not p.exists():
        return 0
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error("Failed to read %s for import: %s", p, e)
        return 0
    now = time.time()
    imported = 0
    for t in data.get("tenants", []) or []:
        cfg = _tenant_from_dict(t)
        if not cfg.id or cfg.id == "default":
            logger.warning("Skipping tenant with missing/invalid id: %s", t)
            continue
        cfg.password = passwords.ensure_hashed(cfg.password)
        cfg.call_key = cfg.call_key or secrets.token_hex(16)
        cfg.created_at = cfg.created_at or now
        cfg.updated_at = now
        _write(cfg)
        imported += 1
    p.replace(p.with_name(p.name + ".imported"))
    logger.info("Imported %d tenants from %s into SQLite", imported, p)
    return imported


def load_tenants(path: str | None = None) -> dict[str, TenantConfig]:
    """All tenants from the table as {id: TenantConfig}, plus the default."""
    import_json_if_empty(path)
    out: dict[str, TenantConfig] = {"default": _default()}
    for row in _conn().execute("SELECT * FROM tenants ORDER BY created_at").fetchall():
        cfg = _row_to_cfg(row)
        out[cfg.id] = cfg
    return out


def reload_tenants() -> dict[str, TenantConfig]:
    """Kept for callers that used to refresh the JSON cache; reads are live now."""
    return load_tenants()


def save_tenants(path: str | None = None) -> None:
    """Write an export copy of every client to JSON (the table is the source)."""
    p = Path(path or os.path.join(os.path.dirname(_TENANTS_PATH), "tenants.export.json"))
    payload = {
        "_comment": "Export of the tenants table. The live config is in calls.db; edit via the admin portal.",
        "default": {"name": DEFAULT_TENANT.name, "language": DEFAULT_TENANT.language,
                    "agent_name": DEFAULT_TENANT.agent_name},
        "tenants": [t.public_dict() for tid, t in load_tenants().items() if tid != "default"],
    }
    with _WRITE_LOCK:
        tmp = p.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        tmp.replace(p)
    logger.info("Exported %d tenants to %s", len(payload["tenants"]), p)


def upsert_tenant(data: dict, *, create_only: bool = False) -> TenantConfig:
    """Create or update a client from a dict, persist, and return the config.

    `id` is derived from the name if absent. Editing keeps the same id.
    A non-empty `password` or a change of `active` bumps `session_version`,
    which logs the client out everywhere (auth checks it on every request).
    """
    incoming_id = slugify((data.get("id") or "").strip() or data.get("name", ""))
    if incoming_id == "default":
        raise ValueError("'default' is reserved")
    existing = _read(incoming_id)
    if existing is not None and create_only:
        raise TenantExists(f"client '{incoming_id}' already exists")
    now = time.time()
    if existing is not None:
        merged = existing.public_dict()
        for k, v in data.items():
            if v is not None:
                merged[k] = v
        cfg = _tenant_from_dict(merged, fallback_id=incoming_id)
        cfg.call_key = existing.call_key or secrets.token_hex(16)
        cfg.created_at = existing.created_at or now
        bump = bool(data.get("password")) or (
            data.get("active") is not None and bool(data["active"]) != existing.active
        )
        cfg.session_version = existing.session_version + (1 if bump else 0)
    else:
        cfg = _tenant_from_dict(data, fallback_id=incoming_id)
        cfg.call_key = secrets.token_hex(16)
        # A random start, so a slug deleted and recreated (even within the same
        # second) never repeats a version an old cookie still carries.
        cfg.session_version = secrets.randbelow(2**31) + 1
        cfg.created_at = now
    cfg.id = incoming_id
    cfg.updated_at = now
    cfg.password = passwords.ensure_hashed(cfg.password)
    _write(cfg)
    return cfg


def bump_session(tenant_id: str) -> int:
    """Invalidate every session of this client. Returns the new version."""
    cfg = _read(slugify(tenant_id))
    if cfg is None:
        raise KeyError(tenant_id)
    cfg.session_version += 1
    cfg.updated_at = time.time()
    _write(cfg)
    return cfg.session_version


def rotate_call_key(tenant_id: str) -> str:
    """Issue a new public call key; old embeds stop working at once."""
    cfg = _read(slugify(tenant_id))
    if cfg is None:
        raise KeyError(tenant_id)
    cfg.call_key = secrets.token_hex(16)
    cfg.updated_at = time.time()
    _write(cfg)
    return cfg.call_key


def delete_tenant(tenant_id: str) -> bool:
    """Remove a client. Returns True if it existed."""
    tid = slugify(tenant_id)
    if tid == "default":
        raise ValueError("cannot delete the default tenant")
    with _WRITE_LOCK:
        cur = _conn().execute("DELETE FROM tenants WHERE id = ?", (tid,))
        if cur.rowcount:
            # Keep the history, but under an id no new client can be given, so a
            # client created later with the same slug never sees it.
            archive = f"{tid}#deleted-{int(time.time())}"
            _conn().execute("UPDATE calls SET client_id = ? WHERE client_id = ?", (archive, tid))
            _conn().execute("UPDATE lead_deliveries SET client_id = ? WHERE client_id = ?", (archive, tid))
        _conn().commit()
    return cur.rowcount > 0


def list_tenants(include_default: bool = False) -> list[dict]:
    """Return tenants as a list of dicts (for the admin API — secrets stripped)."""
    return [
        t.safe_dict()
        for tid, t in load_tenants().items()
        if include_default or tid != "default"
    ]


def get_tenant(tenant_id: str) -> TenantConfig | None:
    """Look up a client by id slug (case and spacing tolerant). None if unknown."""
    if not tenant_id:
        return None
    tid = slugify(tenant_id)
    if tid == "default":
        return _default()
    return _read(tid)


def get_tenant_for_number(phone_number: str) -> TenantConfig:
    """Look up tenant by called phone number (SIP). Returns default if unknown."""
    if not phone_number:
        return _default()
    num = _normalize_number(phone_number)
    digits = "".join(c for c in num if c.isdigit())
    # Paused clients still own their number: the agent declines the call as
    # them rather than answering as the demo agent.
    rows = _conn().execute("SELECT * FROM tenants WHERE phone_number != ''").fetchall()
    for row in rows:
        t = _row_to_cfg(row)
        t_digits = "".join(c for c in t.phone_number if c.isdigit())
        if t_digits and t_digits == digits:
            return t
    logger.info("No tenant for number %r — using default", phone_number)
    return _default()


# LiveKit SIP sets these attributes on the caller's participant.
SIP_CALLED_ATTR = "sip.trunkPhoneNumber"   # the number that was dialled (ours)
SIP_CALLER_ATTR = "sip.phoneNumber"        # the caller's number


def resolve_tenant(metadata, attributes: dict | None = None) -> tuple[TenantConfig, str]:
    """Which client a call belongs to, and who is calling.

    Order: a tenant named in the room metadata (browser calls, or a dispatch
    rule that pins one client) wins; otherwise the dialled number on the SIP
    participant picks the client; otherwise the default agent answers. The
    caller's number is returned for the call record ("" for browser calls)."""
    attrs = attributes or {}
    caller = (attrs.get(SIP_CALLER_ATTR) or "").strip()
    called = (attrs.get(SIP_CALLED_ATTR) or "").strip()
    tenant = get_tenant_from_metadata(metadata)
    if tenant.id == "default" and called:
        tenant = get_tenant_for_number(called)
    return tenant, caller


def get_tenant_from_metadata(metadata) -> TenantConfig:
    """Resolve tenant from LiveKit room metadata.

    Expected JSON shapes (tried in order):
      {"tenant": "<id-slug>"}                ← server token endpoint / SIP dispatch
      {"client_id": "<id-slug>"}             ← alias
      {"phone_number": "+91XXXXXXXXXX"}      ← SIP called-number routing (later)
      {"language": "te"}                     ← browser-test shortcut (no client)

    Anything else → default.
    """
    if not metadata:
        return _default()

    if isinstance(metadata, (bytes, bytearray)):
        try:
            metadata = metadata.decode("utf-8", errors="ignore")
        except Exception:
            return _default()

    try:
        data = json.loads(metadata) if isinstance(metadata, str) else metadata
    except Exception:
        return _default()

    if not isinstance(data, dict):
        return _default()

    # Resolve by client id slug first.
    tid = (data.get("tenant") or data.get("client_id") or "").strip()
    if tid:
        found = get_tenant(tid)
        if found is not None and found.id != "default":
            return found

    # Then by phone number (SIP).
    num = data.get("phone_number") or ""
    if num:
        return get_tenant_for_number(num)

    # Browser-test shortcut: language only, no client attribution.
    lang = data.get("language")
    if lang in ("hi", "te", "en"):
        base = _default()
        return TenantConfig(
            id="default",
            name=base.name,
            language=lang,
            agent_name=base.agent_name,
        )

    return _default()
