"""Durable, owner-scoped proof-of-possession device enrollment.

No remote API, session token, WebAuthn ceremony or platform hardware attestation.
Only an authenticated owner controller may start enrollment or revoke devices.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import Engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from max_core.models import DeviceChallenge, DeviceNode, utc_now

_OWNER = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_LABEL = re.compile(r"[A-Za-z0-9 _.-]{3,64}\Z")
_PLATFORMS = frozenset({"windows", "linux", "ios"})
_TTL_SECONDS = 120


class NodeError(ValueError):
    """Stable safe error code; does not include key or signature material."""


@dataclass(frozen=True, slots=True, repr=False)
class ChallengeTicket:
    device_id: str
    challenge_id: str
    nonce: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class NodeView:
    id: str
    label: str
    platform: str
    state: str
    created_at: datetime
    revoked_at: datetime | None

    @classmethod
    def from_row(cls, node: DeviceNode) -> NodeView:
        return cls(
            node.id,
            node.label,
            node.platform,
            node.state,
            node.created_at,
            node.revoked_at,
        )


def _owner(owner_id: str) -> None:
    if not isinstance(owner_id, str) or _OWNER.fullmatch(owner_id) is None:
        raise NodeError("invalid_owner")


def _decode(value: str, *, ceiling: int) -> bytes:
    if not isinstance(value, str) or len(value) > ceiling or not value:
        raise NodeError("invalid_signature_encoding")
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
            raise ValueError("noncanonical base64url")
        return raw
    except (ValueError, UnicodeError):
        raise NodeError("invalid_signature_encoding") from None


def _public_key(encoded: str) -> ec.EllipticCurvePublicKey:
    try:
        raw = _decode(encoded, ceiling=512)
        loaded = serialization.load_der_public_key(raw)
        if not isinstance(loaded, ec.EllipticCurvePublicKey) or not isinstance(
            loaded.curve, ec.SECP256R1
        ):
            raise NodeError("unsupported_device_key")
        if (
            loaded.public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            != raw
        ):
            raise NodeError("noncanonical_device_key")
        return loaded
    except (ValueError, TypeError):
        raise NodeError("invalid_device_key") from None


def challenge_payload(
    owner_id: str, device_id: str, challenge_id: str, purpose: str, nonce: str
) -> bytes:
    """Versioned, domain-separated bytes that the device signs with ECDSA/SHA256."""
    _owner(owner_id)
    if purpose not in {"enroll", "authenticate"}:
        raise NodeError("invalid_challenge_purpose")
    if not all(
        isinstance(x, str) and re.fullmatch(r"[0-9a-f-]{36}", x) for x in (device_id, challenge_id)
    ):
        raise NodeError("invalid_challenge_identity")
    if len(_decode(nonce, ceiling=64)) != 32:
        raise NodeError("invalid_challenge_nonce")
    return b"MAX-NODE-P256-V1\x00" + b"\x00".join(
        part.encode("ascii") for part in (owner_id, device_id, challenge_id, purpose, nonce)
    )


class NodeRegistry:
    """Device keys never leave their OS/TPM/Secure Enclave owner environment.

    A valid signature proves possession of the registered private key, NOT
    hardware backing or owner presence. Do not treat it as WebAuthn step-up.
    """

    def __init__(self, engine: Engine) -> None:
        self.sessions = sessionmaker(engine, expire_on_commit=False)

    @staticmethod
    def _locked(session: Session, owner_id: str, device_id: str) -> DeviceNode:
        node = session.scalar(
            select(DeviceNode)
            .where(DeviceNode.id == device_id, DeviceNode.owner_id == owner_id)
            .with_for_update()
        )
        if node is None:
            raise NodeError("node_not_found")
        return node

    @staticmethod
    def _ticket(session: Session, node: DeviceNode, purpose: str) -> ChallengeTicket:
        nonce = secrets.token_urlsafe(32)
        challenge = DeviceChallenge(
            id=str(uuid4()),
            owner_id=node.owner_id,
            device_id=node.id,
            purpose=purpose,
            nonce_sha256=hashlib.sha256(nonce.encode("ascii")).hexdigest(),
            state="pending",
            expires_at=utc_now() + timedelta(seconds=_TTL_SECONDS),
        )
        session.add(challenge)
        return ChallengeTicket(node.id, challenge.id, nonce, challenge.expires_at)

    def begin_enrollment(
        self, owner_id: str, label: str, platform: str, public_key_der_b64url: str
    ) -> ChallengeTicket:
        """Trusted owner initiation only; caller MUST enforce real owner auth."""
        _owner(owner_id)
        if (
            not isinstance(label, str)
            or _LABEL.fullmatch(label) is None
            or platform not in _PLATFORMS
        ):
            raise NodeError("invalid_node_metadata")
        public_key = _public_key(public_key_der_b64url)
        canonical = public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        node = DeviceNode(
            id=str(uuid4()),
            owner_id=owner_id,
            label=label,
            platform=platform,
            public_key_b64url=public_key_der_b64url,
            key_sha256=hashlib.sha256(canonical).hexdigest(),
            state="pending",
        )
        try:
            with self.sessions.begin() as session:
                session.add(node)
                session.flush()
                ticket = self._ticket(session, node, "enroll")
            return ticket
        except IntegrityError:
            raise NodeError("duplicate_node_or_key") from None

    def _verify(
        self,
        owner_id: str,
        ticket: ChallengeTicket,
        signature_b64url: str,
        *,
        purpose: str,
    ) -> NodeView:
        _owner(owner_id)
        if not isinstance(ticket, ChallengeTicket):
            raise NodeError("invalid_challenge")
        signature = _decode(signature_b64url, ceiling=512)
        with self.sessions.begin() as session:
            node = self._locked(session, owner_id, ticket.device_id)
            if node.state != ("pending" if purpose == "enroll" else "active"):
                raise NodeError("device_inactive")
            challenge = session.scalar(
                select(DeviceChallenge)
                .where(
                    DeviceChallenge.id == ticket.challenge_id,
                    DeviceChallenge.owner_id == owner_id,
                    DeviceChallenge.device_id == node.id,
                    DeviceChallenge.purpose == purpose,
                )
                .with_for_update()
            )
            if challenge is None or challenge.state != "pending":
                raise NodeError("challenge_not_pending")
            expiry = challenge.expires_at
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if expiry <= utc_now():
                raise NodeError("challenge_expired")
            digest = hashlib.sha256(ticket.nonce.encode("ascii")).hexdigest()
            if not compare_digest(challenge.nonce_sha256, digest):
                raise NodeError("challenge_binding_mismatch")
            payload = challenge_payload(owner_id, node.id, challenge.id, purpose, ticket.nonce)
            try:
                _public_key(node.public_key_b64url).verify(
                    signature, payload, ec.ECDSA(hashes.SHA256())
                )
            except InvalidSignature:
                raise NodeError("invalid_device_signature") from None
            challenge.state = "consumed"
            if purpose == "enroll":
                node.state = "active"
            session.flush()
            return NodeView.from_row(node)

    def finish_enrollment(
        self, owner_id: str, ticket: ChallengeTicket, signature_b64url: str
    ) -> NodeView:
        return self._verify(owner_id, ticket, signature_b64url, purpose="enroll")

    def begin_authentication(self, owner_id: str, device_id: str) -> ChallengeTicket:
        """Proof-of-possession challenge; NOT a session or HTTP authorization."""
        _owner(owner_id)
        with self.sessions.begin() as session:
            node = self._locked(session, owner_id, device_id)
            if node.state != "active":
                raise NodeError("device_inactive")
            return self._ticket(session, node, "authenticate")

    def verify_authentication(
        self, owner_id: str, ticket: ChallengeTicket, signature_b64url: str
    ) -> NodeView:
        return self._verify(owner_id, ticket, signature_b64url, purpose="authenticate")

    def revoke(self, owner_id: str, device_id: str) -> NodeView:
        """Only the authenticated owner controller may revoke."""
        _owner(owner_id)
        with self.sessions.begin() as session:
            node = self._locked(session, owner_id, device_id)
            if node.state == "revoked":
                return NodeView.from_row(node)
            node.state = "revoked"
            node.revoked_at = utc_now()
            session.execute(
                update(DeviceChallenge)
                .where(
                    DeviceChallenge.owner_id == owner_id,
                    DeviceChallenge.device_id == node.id,
                    DeviceChallenge.state == "pending",
                )
                .values(state="revoked")
            )
            session.flush()
            return NodeView.from_row(node)

    def list_nodes(self, owner_id: str) -> tuple[NodeView, ...]:
        _owner(owner_id)
        with self.sessions() as session:
            nodes = session.scalars(
                select(DeviceNode)
                .where(DeviceNode.owner_id == owner_id)
                .order_by(DeviceNode.created_at, DeviceNode.id)
            ).all()
            return tuple(NodeView.from_row(node) for node in nodes)
