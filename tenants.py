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
"""
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path

import passwords

logger = logging.getLogger("tenants")

# Serializes writes so concurrent admin edits don't corrupt tenants.json.
_WRITE_LOCK = threading.Lock()


# ── Per-tenant (per-client) config ──────────────────────────────────────────
# `language` is one of:
#   "hi"  — Hinglish (Roman-script Hindi)  ← default
#   "te"  — Telugu
#   "en"  — English
# The TTS service picks a native voice for each language automatically.
@dataclass
class TenantConfig:
    id: str = "default"                 # stable slug — primary key & login username
    name: str = ""                      # Agency name (display)
    language: str = "hi"                # "hi" | "te" | "en"
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

# ── Loader ─────────────────────────────────────────────────────────────────

_TENANTS_PATH = os.environ.get(
    "TENANTS_CONFIG",
    os.path.join(os.path.dirname(__file__), "tenants.json"),
)


def slugify(text: str) -> str:
    """Turn an agency name into a stable id slug."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or "client"


def _normalize_number(num: str) -> str:
    """Normalize phone number for matching: keep digits and a leading +."""
    if not num:
        return ""
    return str(num).strip().replace("tel:", "").replace("sip:", "")


def _tenant_from_dict(d: dict, *, fallback_id: str = "") -> TenantConfig:
    """Build a TenantConfig from a raw dict, tolerating missing keys."""
    tid = (d.get("id") or fallback_id or slugify(d.get("name", ""))).strip()
    return TenantConfig(
        id=tid,
        name=d.get("name", ""),
        language=d.get("language", "hi"),
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
    )


def load_tenants(path: str | None = None) -> dict[str, TenantConfig]:
    """Load tenant config from JSON. Returns {id: TenantConfig}.

    File format:
      {
        "default": {"name": "Default", "language": "hi", "agent_name": "Riya"},
        "tenants": [
          {"id": "mumbai-realty", "name": "Mumbai Realty", "language": "hi",
           "agent_name": "Riya", "system_prompt": "...", "password": "..."},
          ...
        ]
      }
    """
    p = Path(path or _TENANTS_PATH)
    if not p.exists():
        logger.warning("tenants.json not found at %s — using default only", p)
        return {"default": DEFAULT_TENANT}

    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error("Failed to load tenants.json: %s — using default only", e)
        return {"default": DEFAULT_TENANT}

    out: dict[str, TenantConfig] = {}

    default_cfg = data.get("default", {}) or {}
    out["default"] = _tenant_from_dict(default_cfg, fallback_id="default")
    out["default"].id = "default"

    for t in data.get("tenants", []) or []:
        cfg = _tenant_from_dict(t)
        if not cfg.id or cfg.id == "default":
            logger.warning("Skipping tenant with missing/invalid id: %s", t)
            continue
        out[cfg.id] = cfg

    logger.info(
        "Loaded %d tenants from %s (default language=%s, agent=%s)",
        len(out), p, out["default"].language, out["default"].agent_name,
    )
    return out


# Module-level registry, loaded at import
_TENANTS: dict[str, TenantConfig] = load_tenants()


def reload_tenants() -> dict[str, TenantConfig]:
    """Hot-reload tenant config (call after editing tenants.json)."""
    global _TENANTS
    _TENANTS = load_tenants()
    return _TENANTS


# ── Persistence (admin CRUD) ─────────────────────────────────────────────────

def save_tenants(path: str | None = None) -> None:
    """Write the current registry back to JSON (default + all real clients)."""
    p = Path(path or _TENANTS_PATH)
    default = _TENANTS.get("default", DEFAULT_TENANT)
    payload = {
        "_comment": (
            "Multi-tenant config. Each tenant = one business client. "
            "Edited via the admin portal (POST/PUT/DELETE /api/admin/clients) or by hand. "
            "Restart agent.py to pick up changes for live calls; server hot-reloads."
        ),
        "default": {
            "name": default.name,
            "language": default.language,
            "agent_name": default.agent_name,
        },
        "tenants": [
            t.public_dict() for tid, t in _TENANTS.items() if tid != "default"
        ],
    }
    with _WRITE_LOCK:
        tmp = p.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        tmp.replace(p)
    logger.info("Saved %d tenants to %s", len(payload["tenants"]), p)


def upsert_tenant(data: dict) -> TenantConfig:
    """Create or update a client from a dict, persist, and return the config.

    `id` is derived from the name if absent. Editing keeps the same id.
    Blank password/fields on update preserve the existing value where sensible.
    """
    incoming_id = (data.get("id") or "").strip()
    if not incoming_id:
        incoming_id = slugify(data.get("name", ""))
    if incoming_id == "default":
        raise ValueError("'default' is reserved")

    existing = _TENANTS.get(incoming_id)
    if existing is not None:
        # Merge: only overwrite keys actually provided (non-None).
        merged = existing.public_dict()
        for k, v in data.items():
            if v is not None:
                merged[k] = v
        cfg = _tenant_from_dict(merged, fallback_id=incoming_id)
    else:
        cfg = _tenant_from_dict(data, fallback_id=incoming_id)

    cfg.id = incoming_id
    # Never persist a password in the clear — hashing here covers both the admin
    # portal and any hand-edited tenants.json that gets re-saved.
    cfg.password = passwords.ensure_hashed(cfg.password)
    _TENANTS[incoming_id] = cfg
    save_tenants()
    return cfg


def delete_tenant(tenant_id: str) -> bool:
    """Remove a client. Returns True if it existed."""
    if tenant_id == "default":
        raise ValueError("cannot delete the default tenant")
    if tenant_id in _TENANTS:
        del _TENANTS[tenant_id]
        save_tenants()
        return True
    return False


# ── Lookups ──────────────────────────────────────────────────────────────────

def list_tenants(include_default: bool = False) -> list[dict]:
    """Return tenants as a list of dicts (for the admin API — secrets stripped)."""
    return [
        t.safe_dict()
        for tid, t in _TENANTS.items()
        if include_default or tid != "default"
    ]


def get_tenant(tenant_id: str) -> TenantConfig | None:
    """Look up a client by id slug. Returns None if unknown."""
    return _TENANTS.get(tenant_id)


def get_tenant_for_number(phone_number: str) -> TenantConfig:
    """Look up tenant by called phone number (SIP). Returns default if unknown."""
    if not phone_number:
        return _TENANTS.get("default", DEFAULT_TENANT)
    num = _normalize_number(phone_number)
    digits = "".join(c for c in num if c.isdigit())
    for t in _TENANTS.values():
        if t.id == "default" or not t.phone_number:
            continue
        t_digits = "".join(c for c in t.phone_number if c.isdigit())
        if t_digits and t_digits == digits:
            return t
    logger.info("No tenant for number %r — using default", phone_number)
    return _TENANTS.get("default", DEFAULT_TENANT)


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
        return _TENANTS.get("default", DEFAULT_TENANT)

    if isinstance(metadata, (bytes, bytearray)):
        try:
            metadata = metadata.decode("utf-8", errors="ignore")
        except Exception:
            return _TENANTS.get("default", DEFAULT_TENANT)

    try:
        data = json.loads(metadata) if isinstance(metadata, str) else metadata
    except Exception:
        return _TENANTS.get("default", DEFAULT_TENANT)

    if not isinstance(data, dict):
        return _TENANTS.get("default", DEFAULT_TENANT)

    # Resolve by client id slug first.
    tid = (data.get("tenant") or data.get("client_id") or "").strip()
    if tid and tid in _TENANTS:
        return _TENANTS[tid]

    # Then by phone number (SIP).
    num = data.get("phone_number") or ""
    if num:
        return get_tenant_for_number(num)

    # Browser-test shortcut: language only, no client attribution.
    lang = data.get("language")
    if lang in ("hi", "te", "en"):
        base = _TENANTS.get("default", DEFAULT_TENANT)
        return TenantConfig(
            id="default",
            name=base.name,
            language=lang,
            agent_name=base.agent_name,
        )

    return _TENANTS.get("default", DEFAULT_TENANT)
