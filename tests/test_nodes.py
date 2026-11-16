"""Offline NodeRegistry security tests; no real devices or network."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import create_engine, select

from max_core.models import Base, DeviceChallenge
from max_core.nodes import NodeError, NodeRegistry, challenge_payload


@pytest.fixture
def registry() -> NodeRegistry:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return NodeRegistry(engine)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def new_key() -> tuple[ec.EllipticCurvePrivateKey, str]:
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, b64(public)


def sign(
    private: ec.EllipticCurvePrivateKey, owner: str, ticket: object, purpose: str
) -> str:
    from max_core.nodes import ChallengeTicket

    assert isinstance(ticket, ChallengeTicket)
    return b64(
        private.sign(
            challenge_payload(
                owner, ticket.device_id, ticket.challenge_id, purpose, ticket.nonce
            ),
            ec.ECDSA(hashes.SHA256()),
        )
    )


def test_enroll_authenticate_and_replay_deny(registry: NodeRegistry) -> None:
    private, pub = new_key()
    ticket = registry.begin_enrollment("owner", "Windows 11", "windows", pub)
    assert registry.list_nodes("owner")[0].state == "pending"
    assert registry.list_nodes("other") == ()
    node = registry.finish_enrollment("owner", ticket, sign(private, "owner", ticket, "enroll"))
    assert node.state == "active"
    with pytest.raises(NodeError, match="challenge_not_pending"):
        registry.finish_enrollment("owner", ticket, sign(private, "owner", ticket, "enroll"))
    auth = registry.begin_authentication("owner", node.id)
    assert registry.verify_authentication(
        "owner", auth, sign(private, "owner", auth, "authenticate")
    ).state == "active"
    with pytest.raises(NodeError, match="challenge_not_pending"):
        registry.verify_authentication(
            "owner", auth, sign(private, "owner", auth, "authenticate")
        )


def test_forged_key_wrong_owner_and_purpose_denied(registry: NodeRegistry) -> None:
    private, pub = new_key()
    wrong_private, _ = new_key()
    ticket = registry.begin_enrollment("owner", "Arch Linux", "linux", pub)
    with pytest.raises(NodeError, match="node_not_found"):
        registry.finish_enrollment("other", ticket, sign(private, "other", ticket, "enroll"))
    with pytest.raises(NodeError, match="invalid_device_signature"):
        registry.finish_enrollment(
            "owner", ticket, sign(wrong_private, "owner", ticket, "enroll")
        )
    with pytest.raises(NodeError, match="invalid_device_signature"):
        registry.finish_enrollment(
            "owner", ticket, sign(private, "owner", ticket, "authenticate")
        )
    assert registry.finish_enrollment(
        "owner", ticket, sign(private, "owner", ticket, "enroll")
    ).state == "active"


def test_revoke_invalidates_pending_authentication(registry: NodeRegistry) -> None:
    private, pub = new_key()
    ticket = registry.begin_enrollment("owner", "iPhone 17", "ios", pub)
    node = registry.finish_enrollment("owner", ticket, sign(private, "owner", ticket, "enroll"))
    auth = registry.begin_authentication("owner", node.id)
    registry.revoke("owner", node.id)
    with pytest.raises(NodeError, match="device_inactive"):
        registry.verify_authentication(
            "owner", auth, sign(private, "owner", auth, "authenticate")
        )
    with pytest.raises(NodeError, match="device_inactive"):
        registry.begin_authentication("owner", node.id)


def test_expired_challenge_is_rejected(registry: NodeRegistry) -> None:
    private, pub = new_key()
    ticket = registry.begin_enrollment("owner", "Arch Linux", "linux", pub)
    with registry.sessions.begin() as session:
        challenge = session.scalar(
            select(DeviceChallenge).where(DeviceChallenge.id == ticket.challenge_id)
        )
        assert challenge is not None
        challenge.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(NodeError, match="challenge_expired"):
        registry.finish_enrollment("owner", ticket, sign(private, "owner", ticket, "enroll"))


def test_duplicate_key_and_unsupported_curve_denied(registry: NodeRegistry) -> None:
    _, pub = new_key()
    registry.begin_enrollment("owner", "Arch Linux", "linux", pub)
    with pytest.raises(NodeError, match="duplicate_node_or_key"):
        registry.begin_enrollment("owner", "Windows 11", "windows", pub)
    another = ec.generate_private_key(ec.SECP384R1())
    encoded = b64(
        another.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    with pytest.raises(NodeError, match="unsupported_device_key"):
        registry.begin_enrollment("owner", "Another Node", "linux", encoded)


def test_malformed_signature_denied(registry: NodeRegistry) -> None:
    _, pub = new_key()
    ticket = registry.begin_enrollment("owner", "Arch Linux", "linux", pub)
    with pytest.raises(NodeError, match="invalid_signature_encoding"):
        registry.finish_enrollment("owner", ticket, "$$invalid$$")


def test_postgres_nodes_roundtrip(postgres_store: object) -> None:
    engine = postgres_store.engine  # type: ignore[attr-defined]
    registry = NodeRegistry(engine)
    private, pub = new_key()
    ticket = registry.begin_enrollment("owner", "Staging test node", "linux", pub)
    node = registry.finish_enrollment("owner", ticket, sign(private, "owner", ticket, "enroll"))
    assert node.state == "active"
    assert registry.revoke("owner", node.id).state == "revoked"
