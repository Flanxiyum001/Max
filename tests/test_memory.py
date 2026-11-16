"""No network, model, personal data or live memory service involved."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select

from max_core.memory import MemoryStore, MemoryStoreError
from max_core.models import Base, MemoryEvent, MemoryRecord

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SOURCE = sha256(b"memory-test-provenance").hexdigest()


@pytest.fixture
def memory() -> MemoryStore:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return MemoryStore(engine)


def propose(
    store: MemoryStore,
    *,
    owner: str = "owner-a",
    scope: str = "personal",
    key: str = "os.preference",
    value: str = "Linux",
    source: str = "inferred_from_task",
    confidence: int = 800,
    when: datetime = NOW,
) -> str:
    view = store.propose(
        owner_id=owner,
        scope=scope,
        subject_key=key,
        content=value,
        source_kind=source,
        source_sha256=SOURCE,
        confidence=confidence,
        now=when,
    )
    assert view.state == "pending"
    return view.id


def test_candidates_are_not_facts_until_owner_review(memory: MemoryStore) -> None:
    ident = propose(memory)
    assert memory.retrieve("owner-a", scope="personal", now=NOW) == ()
    pending = memory.review_queue("owner-a", scope="personal", now=NOW)
    assert len(pending) == 1
    assert pending[0].id == ident
    approved = memory.review("owner-a", ident, expected_revision=1, approve=True, now=NOW)
    assert approved.revision == 2
    result = memory.retrieve("owner-a", scope="personal", now=NOW)
    assert len(result) == 1
    assert result[0].memory.content == "Linux"
    assert result[0].effective_confidence == 800
    assert memory.review_queue("owner-a", scope="personal", now=NOW) == ()


def test_decay_and_expiry_never_strengthen_inferences(memory: MemoryStore) -> None:
    ident = propose(memory, confidence=1000)
    memory.review("owner-a", ident, expected_revision=1, approve=True, now=NOW)
    month = memory.retrieve("owner-a", scope="personal", now=NOW + timedelta(days=30))
    assert month[0].effective_confidence == 500
    assert memory.retrieve(
        "owner-a", scope="personal", minimum_confidence=501, now=NOW + timedelta(days=30)
    ) == ()
    assert memory.retrieve("owner-a", scope="personal", now=NOW + timedelta(days=90)) == ()


def test_explicit_user_correction_does_not_decay(memory: MemoryStore) -> None:
    ident = propose(memory)
    memory.review("owner-a", ident, expected_revision=1, approve=True, now=NOW)
    changed = memory.correct(
        "owner-a", ident, expected_revision=2, content="Arch Linux",
        source_sha256=SOURCE, now=NOW + timedelta(days=4)
    )
    assert changed.revision == 3
    assert changed.source_kind == "explicit_user_correction"
    assert changed.expires_at is None
    result = memory.retrieve("owner-a", scope="personal", now=NOW + timedelta(days=3650))
    assert len(result) == 1
    assert result[0].effective_confidence == 1000
    assert result[0].memory.content == "Arch Linux"
    assert "Linux" not in repr(changed)
    assert [event.action for event in memory.audit("owner-a", ident)] == [
        "proposed", "approved", "corrected"
    ]


def test_rejections_do_not_enter_retrieval(memory: MemoryStore) -> None:
    ident = propose(memory)
    memory.review("owner-a", ident, expected_revision=1, approve=False, now=NOW)
    assert memory.retrieve("owner-a", scope="personal", now=NOW) == ()
    with pytest.raises(MemoryStoreError, match="stale_memory_review"):
        memory.review("owner-a", ident, expected_revision=1, approve=True, now=NOW)


def test_owner_scope_isolation_on_every_read_and_mutation(memory: MemoryStore) -> None:
    ident = propose(memory)
    assert memory.review_queue("owner-b", scope="personal", now=NOW) == ()
    assert memory.review_queue("owner-a", scope="work", now=NOW) == ()
    for action in (
        lambda: memory.review("owner-b", ident, expected_revision=1, approve=True),
        lambda: memory.correct(
            "owner-b", ident, expected_revision=1, content="stolen", source_sha256=SOURCE
        ),
        lambda: memory.delete("owner-b", ident, expected_revision=1),
        lambda: memory.audit("owner-b", ident),
    ):
        with pytest.raises(MemoryStoreError, match="memory_not_found"):
            action()
    assert memory.retrieve("owner-b", scope="personal", now=NOW) == ()


def test_optimistic_revision_and_uniqueness(memory: MemoryStore) -> None:
    ident = propose(memory)
    with pytest.raises(MemoryStoreError, match="memory_slot_exists"):
        propose(memory, value="contradictory")
    memory.review("owner-a", ident, expected_revision=1, approve=True, now=NOW)
    with pytest.raises(MemoryStoreError, match="stale_memory_correction"):
        memory.correct(
            "owner-a", ident, expected_revision=1, content="wrong",
            source_sha256=SOURCE, now=NOW
        )
    with pytest.raises(MemoryStoreError, match="stale_memory_delete"):
        memory.delete("owner-a", ident, expected_revision=1)


def test_hard_deletion_erases_content_and_revision_audit(memory: MemoryStore) -> None:
    ident = propose(memory, value="erase this private candidate")
    memory.review("owner-a", ident, expected_revision=1, approve=True, now=NOW)
    memory.delete("owner-a", ident, expected_revision=2)
    with memory.sessions() as session:
        assert session.scalar(select(func.count()).select_from(MemoryRecord)) == 0
        assert session.scalar(select(func.count()).select_from(MemoryEvent)) == 0
    with pytest.raises(MemoryStoreError, match="memory_not_found"):
        memory.audit("owner-a", ident)
    assert memory.retrieve("owner-a", scope="personal", now=NOW) == ()
    propose(memory, value="replacement after deletion")


def test_review_queue_returns_five_recent_candidates(memory: MemoryStore) -> None:
    for index in range(7):
        propose(
            memory, key=f"learned.fact{index}", value=f"fact{index}",
            when=NOW + timedelta(days=index)
        )
    queue = memory.review_queue(
        "owner-a", scope="personal", now=NOW + timedelta(days=7), days=7, limit=5
    )
    assert len(queue) == 5
    assert [x.subject_key for x in queue] == [
        "learned.fact6", "learned.fact5", "learned.fact4", "learned.fact3",
        "learned.fact2"
    ]


@pytest.mark.parametrize(
    ("bad", "code"),
    [
        ({"owner_id": "../other"}, "invalid_owner"),
        ({"scope": "private/other"}, "invalid_scope"),
        ({"subject_key": "bad key"}, "invalid_subject_key"),
        ({"content": ""}, "invalid_memory_content"),
        ({"source_kind": "model_guessed"}, "invalid_memory_source"),
        ({"source_sha256": "not-a-hash"}, "invalid_source_digest"),
        ({"confidence": True}, "invalid_memory_confidence"),
        ({"confidence": 1001}, "invalid_memory_confidence"),
    ],
)
def test_invalid_input_fail_closed(
    memory: MemoryStore, bad: dict[str, object], code: str
) -> None:
    kwargs: dict[str, object] = {
        "owner_id": "owner-a",
        "scope": "personal",
        "subject_key": "learned.fact",
        "content": "safe fixture",
        "source_kind": "external_doc",
        "source_sha256": SOURCE,
        "confidence": 800,
        "now": NOW,
    }
    kwargs.update(bad)
    with pytest.raises(MemoryStoreError, match=code):
        memory.propose(**kwargs)  # type: ignore[arg-type]


def test_postgres_migration_and_isolation(postgres_store: object) -> None:
    # Existing CI fixture is migrated through Alembic before pytest starts.
    engine = postgres_store.engine  # type: ignore[attr-defined]
    memory = MemoryStore(engine)
    scope = "test-" + uuid4().hex
    ident = propose(memory, scope=scope)
    memory.review("owner-a", ident, expected_revision=1, approve=True, now=NOW)
    assert len(memory.retrieve("owner-a", scope=scope, now=NOW)) == 1
    assert memory.retrieve("owner-b", scope=scope, now=NOW) == ()
    memory.delete("owner-a", ident, expected_revision=2)
