"""Encrypted, owner-only MCP OAuth credential vault.

One application-level MAX_WEB_SECRET encrypts all app connections; no
per-server environment variables and no plaintext token files. Persistent DB
is optional: MAX_DATABASE_URL (or MAX_MCP_VAULT_DATABASE_URL) points to a durable
database; otherwise MAX_DATA_DIR holds local SQLite (ephemeral on Render free).
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Column, MetaData, String, Table, create_engine, delete, select
from sqlalchemy.engine import Engine

_ALLOWED = frozenset({"github", "cloudflare", "railway", "notion", "linear", "sentry", "figma"})
_METADATA = MetaData()
_CREDENTIALS = Table(
    "max_mcp_oauth_connections",
    _METADATA,
    Column("service", String(32), primary_key=True),
    Column("encrypted", String(65536), nullable=False),
)


class VaultError(ValueError):
    """Fixed error code only; never include provider data or credentials."""


class MCPCredentialVault:
    def __init__(self, env: Mapping[str, str], *, engine: Engine | None = None) -> None:
        secret = env.get("MAX_WEB_SECRET", "")
        self._fernet: Fernet | None = None
        self._engine: Engine | None = None
        self._durable = False
        # Application-wide secret provisioned ONCE by Render, not per MCP app.
        if len(secret) < 32:
            return
        digest = hashlib.sha256(b"MAX-MCP-VAULT-v1\0" + secret.encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))
        try:
            if engine is not None:
                self._engine = engine
                self._durable = True
            else:
                url = env.get("MAX_MCP_VAULT_DATABASE_URL") or env.get("MAX_DATABASE_URL")
                if url:
                    self._durable = True
                else:
                    path = Path(env.get("MAX_DATA_DIR") or "~/.max").expanduser()
                    path.mkdir(parents=True, exist_ok=True, mode=0o700)
                    url = "sqlite:///" + str(path / "mcp-vault.sqlite3")
                    self._durable = "RENDER_EXTERNAL_URL" not in env
                # This is a trusted HOST configuration, never read from model output.
                self._engine = create_engine(url, pool_pre_ping=True)
            _METADATA.create_all(self._engine, tables=[_CREDENTIALS])
        except Exception:
            # Fail closed to memory-only rather than losing existing chat access.
            self._engine = None
            self._durable = False

    @property
    def available(self) -> bool:
        return self._fernet is not None and self._engine is not None

    @property
    def durable(self) -> bool:
        return self.available and self._durable

    def save(self, service: str, data: dict[str, Any]) -> None:
        if service not in _ALLOWED or not self.available:
            return
        assert self._fernet is not None and self._engine is not None
        payload = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload) > 60_000:
            raise VaultError("credential_payload_too_large")
        encrypted = self._fernet.encrypt(payload.encode()).decode()
        with self._engine.begin() as connection:
            connection.execute(delete(_CREDENTIALS).where(_CREDENTIALS.c.service == service))
            connection.execute(_CREDENTIALS.insert().values(service=service, encrypted=encrypted))

    def load(self, service: str) -> dict[str, Any] | None:
        if service not in _ALLOWED or not self.available:
            return None
        assert self._fernet is not None and self._engine is not None
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    select(_CREDENTIALS.c.encrypted).where(_CREDENTIALS.c.service == service)
                ).scalar_one_or_none()
            if row is None:
                return None
            item = json.loads(self._fernet.decrypt(row.encode()).decode())
            return item if isinstance(item, dict) else None
        except (InvalidToken, ValueError, TypeError, UnicodeDecodeError):
            # Wrong/rotated application secret: never use unverified credentials.
            return None

    def delete(self, service: str) -> None:
        if service not in _ALLOWED or not self.available:
            return
        assert self._engine is not None
        with self._engine.begin() as connection:
            connection.execute(delete(_CREDENTIALS).where(_CREDENTIALS.c.service == service))
