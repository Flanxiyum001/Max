"""Persistent semantic memory: deterministic embeddings, MEMORY.md journal, hybrid recall."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from max_core.gateway.contracts import ToolProposal
from max_core.local_tools import (
    LocalToolError,
    LocalToolExecutor,
    LocalToolRegistry,
)
from max_core.memory_recall import (
    MemoryRecallError,
    MemoryRecallIndex,
    append_memory_entry,
    embed,
    read_memory_entries,
    remove_memory_entry,
    validate_memory_entry,
)
from max_core.models import Base, Task
from max_core.store import StoreError, TaskStore
from max_core.trust import RiskTier, TrustService
from tests.conftest import TEST_TOKEN


@pytest.fixture
def storage(tmp_path: Path) -> tuple[TaskStore, Path, Path, Path]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'max-recall-tests.sqlite3'}")
    Base.metadata.create_all(engine)
    store = TaskStore(engine)
    notes = tmp_path / "notes"
    notes.mkdir()
    return store, tmp_path, notes, tmp_path / "max-memory-index.sqlite3"


def test_embed_is_deterministic_unit_norm_and_discriminative() -> None:
    first = embed("Deploy checklist starts with pytest")
    again = embed("Deploy checklist starts with pytest")
    assert first == again
    assert len(first) == 256
    assert abs(math.fsum(value * value for value in first) - 1.0) < 1e-9
    assert embed("totally different topic about glass blowers") != first
    assert embed("") == [0.0] * 256
    with pytest.raises(MemoryRecallError, match="invalid_memory_document"):
        embed(42)  # type: ignore[arg-type]


def test_memory_entry_validation_rejects_bad_content() -> None:
    assert validate_memory_entry("  spaced   fact ") == "spaced fact"
    with pytest.raises(MemoryRecallError, match="invalid_memory_entry"):
        validate_memory_entry("bad\x07fact")
    with pytest.raises(MemoryRecallError, match="invalid_memory_entry"):
        validate_memory_entry("x" * 281)
    with pytest.raises(MemoryRecallError, match="invalid_memory_entry"):
        validate_memory_entry("   ")
    with pytest.raises(MemoryRecallError, match="invalid_memory_entry"):
        validate_memory_entry("# heading")
    with pytest.raises(MemoryRecallError, match="invalid_memory_entry"):
        validate_memory_entry(None)  # type: ignore[arg-type]


def test_append_dedupes_and_returns_counts(storage: tuple) -> None:
    _store, tmp_path, _notes, _index = storage
    md = tmp_path / "custom-MEMORY.md"
    assert append_memory_entry(md, "Prefers Python 3.13 for CI") == {
        "op": "appended",
        "entries": 1,
    }
    assert append_memory_entry(md, "prefers PYTHON 3.13 for ci") == {
        "op": "already_present",
        "entries": 1,
    }
    assert append_memory_entry(md, "Runs pytest before every push") == {
        "op": "appended",
        "entries": 2,
    }
    body = md.read_text(encoding="utf-8")
    assert body.startswith("# MAX memory")
    assert "Prefers Python 3.13 for CI" in body


def test_memory_entry_cap_is_512(storage: tuple) -> None:
    _store, tmp_path, _notes, _index = storage
    md = tmp_path / "MEMORY.md"
    entries = [f"Fact number {index} for the cap check" for index in range(512)]
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("# MAX memory\n" + "".join(f"- {entry}\n" for entry in entries), encoding="utf-8")
    with pytest.raises(MemoryRecallError, match="memory_md_full"):
        append_memory_entry(md, "one more fact")
    assert append_memory_entry(md, "Fact number 5 for the cap check") == {
        "op": "already_present",
        "entries": 512,
    }


def test_remove_memory_entry_and_missing(storage: tuple) -> None:
    _store, tmp_path, _notes, _index = storage
    md = tmp_path / "MEMORY.md"
    append_memory_entry(md, "Keep this fact")
    append_memory_entry(md, "Drop this fact")
    assert remove_memory_entry(md, "drop this fact") == {"op": "removed", "entries": 1}
    assert read_memory_entries(md) == ["Keep this fact"]
    with pytest.raises(MemoryRecallError, match="entry_not_found"):
        remove_memory_entry(md, "never existed")
    with pytest.raises(MemoryRecallError, match="invalid_memory_entry"):
        remove_memory_entry(md, "\x07")


def test_read_memory_entries_skips_headings_and_caps(storage: tuple) -> None:
    _store, tmp_path, _notes, _index = storage
    md = tmp_path / "MEMORY.md"
    md.write_text(
        "# MAX memory\n\n- real entry\nplain continuation line\n# heading\n```\ncode\n```",
        encoding="utf-8",
    )
    assert read_memory_entries(md) == ["real entry", "plain continuation line", "code"]
    broken = tmp_path / "MEMBroken.md"
    broken.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(MemoryRecallError, match="memory_md_unreadable"):
        read_memory_entries(broken)
    assert read_memory_entries(tmp_path / "absent.md") == []


def test_hybrid_index_sync_search_and_rebuild_skip(storage: tuple) -> None:
    _store, tmp_path, notes, index_path = storage
    (notes / "recipe.txt").write_text(
        "Brussel sprouts: roast with garlic at 220C.", encoding="utf-8"
    )
    md = tmp_path / "MEMORY.md"
    append_memory_entry(md, "Style anchor: gravel baking sheet design notes")
    index = MemoryRecallIndex(index_path)
    assert index.vector_ready() is True
    assert index.sync(notes, [("aaaa1111", "Fix the catan tile hex alignment")], md) is True
    assert index.sync(notes, [("aaaa1111", "Fix the catan tile hex alignment")], md) is False

    for query, expected_source in (
        ("brussel sprouts", "note"),
        ("catan tile hex", "task"),
        ("gravel baking sheet", "memory"),
    ):
        hits = index.search(query, limit=3)
        assert hits, query
        assert hits[0].source == expected_source
        assert len(hits[0].snippet) <= 240

    with pytest.raises(MemoryRecallError, match="invalid_memory_query"):
        index.search("brussel sprouts", limit=100)  # type: ignore[arg-type]
    with pytest.raises(MemoryRecallError, match="invalid_memory_query"):
        index.search("   ")
    with pytest.raises(MemoryRecallError, match="invalid_memory_query"):
        index.search("x" * 301)
    with pytest.raises(MemoryRecallError, match="invalid_memory_query"):
        index.search(9)  # type: ignore[arg-type]
    with pytest.raises(MemoryRecallError, match="invalid_memory_query"):
        index.search("valid", limit=9)


def test_index_degrades_to_fts_without_vector_extension(
    storage: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, tmp_path, notes, index_path = storage
    import max_core.memory_recall as module

    monkeypatch.setattr(module, "_load_sqlite_vec", lambda: None)
    index = MemoryRecallIndex(index_path)
    assert index.vector_ready() is False
    md = tmp_path / "MEMORY.md"
    append_memory_entry(md, "Garlic roasting anchor fact")
    assert index.sync(notes, [], md) is True
    hits = index.search("garlic roasting", limit=3)
    assert hits and hits[0].source in {"memory", "note"}


def test_task_store_list_terminal_scopes_and_bounds(storage: tuple) -> None:
    store, _tmp_path, _notes, _index = storage
    first, _ = store.create("owner", "Will finish", "term-first-01")
    second, _ = store.create("owner", "Will stay queued", "term-second-01")
    attacker, _ = store.create("attacker", "Other owner", "term-third-01")
    sessions = sessionmaker(bind=store.engine, expire_on_commit=False)
    with sessions.begin() as session:
        record = session.get(Task, first.id)
        assert record is not None
        record.state = "completed"
    terminal = store.list_terminal("owner")
    assert [view.id for view in terminal] == [first.id]
    assert second.id not in [view.id for view in terminal]
    assert attacker.id not in [view.id for view in terminal]
    assert store.list_terminal("owner", limit=1) == terminal[:1]
    with pytest.raises(StoreError):
        store.list_terminal("owner", limit=0)


AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


def _registry(storage: tuple) -> LocalToolRegistry:
    store, workspace, notes, _index = storage
    return LocalToolRegistry(
        task_store=store,
        workspace_root=workspace,
        notes_dir=notes,
        owner_id="owner",
    )


def test_memory_search_is_r0_read_only_tool(storage: tuple) -> None:
    store, workspace, notes, _index = storage
    registry = _registry(storage)
    spec = registry.get_spec("memory.search")
    assert spec is not None and spec.risk_tier is RiskTier.R0
    assert spec.requires_approval is False
    (notes / "note.txt").write_text("Weekly review happens on Monday mornings.", encoding="utf-8")
    result = registry.execute("memory.search", {"query": "monday morning review"})
    assert result["count"] >= 1
    assert result["vector"] in (True, False)
    hits = result["results"]
    assert hits[0]["source"] in {"note", "task", "memory"}
    assert len(hits[0]["snippet"]) <= 240
    assert "objective" not in str(hits[0])
    registry.execute("memory.propose_update", {"action": "append", "entry": "Reviewer anchor fact"})
    after = registry.execute("memory.search", {"query": "reviewer anchor"})
    assert after["count"] >= 1
    with pytest.raises(LocalToolError):
        registry.execute("memory.search", {"query": ""})
    with pytest.raises(LocalToolError):
        registry.execute("memory.search", {"query": "ok", "limit": 12})


def test_memory_propose_update_is_r1_gated_end_to_end(storage: tuple) -> None:
    store, workspace, notes, _index = storage
    registry = _registry(storage)
    spec = registry.get_spec("memory.propose_update")
    assert spec is not None and spec.risk_tier is RiskTier.R1
    assert spec.requires_approval is True
    executor = LocalToolExecutor(registry=registry, task_store=store)

    requested = executor.execute(
        ToolProposal("call-mem-1", "memory.propose_update", {"action": "append", "entry": ""})
    )
    assert requested.status == "denied"

    proposal = ToolProposal(
        "call-mem-2",
        "memory.propose_update",
        {"action": "append", "entry": "Deploy checklist memory"},
    )
    requested = executor.execute(proposal)
    assert requested.status == "awaiting_approval"
    approval_id = requested.approval_id
    assert approval_id is not None
    TrustService(store.engine, registry=registry.policy_registry()).decide(
        "owner", approval_id, True
    )
    result = executor.execute(proposal, approval_id=approval_id)
    assert result.status == "completed"
    assert result.output["op"] == "appended"  # type: ignore[index]
    md = workspace / "MEMORY.md"
    assert "Deploy checklist memory" in md.read_text(encoding="utf-8")

    replay = executor.execute(
        ToolProposal(
            "call-mem-3",
            "memory.propose_update",
            {"action": "append", "entry": "Deploy checklist memory"},
        )
    )
    assert replay.status == "awaiting_approval"


def test_memory_propose_update_removal_flow(storage: tuple) -> None:
    store, workspace, notes, _index = storage
    registry = _registry(storage)
    executor = LocalToolExecutor(registry=registry, task_store=store)
    registry.execute("memory.propose_update", {"action": "append", "entry": "Temporary fact"})
    proposal = ToolProposal(
        "call-mem-4", "memory.propose_update", {"action": "remove", "entry": "Temporary fact"}
    )
    requested = executor.execute(proposal)
    approval_id = requested.approval_id
    assert approval_id is not None
    TrustService(store.engine, registry=registry.policy_registry()).decide(
        "owner", approval_id, True
    )
    result = executor.execute(proposal, approval_id=approval_id)
    assert result.status == "completed"
    assert result.output["op"] == "removed"  # type: ignore[index]
    md = workspace / "MEMORY.md"
    assert "Temporary fact" not in md.read_text(encoding="utf-8")
