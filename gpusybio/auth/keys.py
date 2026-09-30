"""Per-gateway API keys (spec 12 §2).

Keys are stored hashed (SHA-256) in the database; the plaintext key is shown
once at creation. A gateway with no keys configured allows all requests (auth
is optional); once any key exists, requests must present a valid
``Authorization: Bearer <key>``. Keys are checked **before** queueing.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid

from gpusybio.database.db import SessionFactory
from gpusybio.database.models import GatewayApiKey


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def create_key(db: SessionFactory, gateway_id: str, name: str | None = None) -> tuple[str, str]:
    """Create a key. Returns (key_id, plaintext_key) — plaintext only once."""
    key_id = "key-" + uuid.uuid4().hex[:12]
    plaintext = "gslb_" + secrets.token_urlsafe(28)
    with db() as s:
        s.add(GatewayApiKey(id=key_id, gateway_id=gateway_id, name=name, key_hash=_hash(plaintext)))
    return key_id, plaintext


def list_keys(db: SessionFactory, gateway_id: str) -> list[dict]:
    out = []
    with db() as s:
        for row in s.query(GatewayApiKey).filter_by(gateway_id=gateway_id).order_by(GatewayApiKey.created_at):
            out.append(
                {
                    "id": row.id,
                    "name": row.name,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
            )
    return out


def delete_key(db: SessionFactory, gateway_id: str, key_id: str) -> bool:
    with db() as s:
        row = s.get(GatewayApiKey, key_id)
        if row is None or row.gateway_id != gateway_id:
            return False
        s.delete(row)
        return True


def verify_key(db: SessionFactory, gateway_id: str, presented: str | None) -> bool:
    """True if the gateway allows the request: no keys configured (auth
    optional), or the presented key matches a stored hash.

    The check is a hash-equality lookup (``key_hash`` is indexed) rather than
    loading every key row and comparing in Python — O(1) in the key count."""
    with db() as s:
        if s.query(GatewayApiKey).filter_by(gateway_id=gateway_id).first() is None:
            return True  # auth not configured for this gateway → allow all
    if not presented:
        return False
    presented_hash = _hash(presented)
    with db() as s:
        return (
            s.query(GatewayApiKey)
            .filter_by(gateway_id=gateway_id, key_hash=presented_hash)
            .first()
            is not None
        )


def key_configured(db: SessionFactory, gateway_id: str) -> bool:
    with db() as s:
        return s.query(GatewayApiKey).filter_by(gateway_id=gateway_id).first() is not None
