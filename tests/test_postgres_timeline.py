"""PostgreSQL timeline race: snapshot-capped read-only metadata across task writes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from max_core.models import OutboxEvent
from max_core.store import StoreError, TaskStore
from max_core.timeline import TaskTimeline
from tests.conftest import TEST_TOKEN


def test_postgres_timeline_pagination_immutable_during_concurrent_checkpoint(
    postgres_store: TaskStore,
) -> None:
    task, _ = postgres_store.create(
        "owner", "PostgreSQL timeline fixture", "pg-timeline-" + uuid4().hex
    )
    postgres_store.claim("owner", task.id, "worker-pg")
    timeline = TaskTimeline(postgres_store, TEST_TOKEN)
    first = timeline.page("owner", task.id, limit=1)
    assert first.snapshot_revision == 2 and first.next_cursor
    assert [x.task_revision for x in first.events] == [1]

    barrier = Barrier(2)

    def write_checkpoint() -> None:
        barrier.wait()
        postgres_store.checkpoint(
            "owner",
            task.id,
            "worker-pg",
            "pg-checkpoint-" + uuid4().hex,
            "safe-checkpoint",
            "a" * 64,
        )

    def next_page() -> list[int]:
        barrier.wait()
        return [
            x.task_revision
            for x in timeline.page("owner", task.id, limit=100, cursor=first.next_cursor).events
        ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        write = pool.submit(write_checkpoint)
        read = pool.submit(next_page)
        subsequent = read.result()
        write.result()

    assert subsequent == [2]
    fresh = timeline.page("owner", task.id, limit=100)
    assert [x.task_revision for x in fresh.events] == [1, 2, 3]
    assert fresh.snapshot_revision == 3

    with Session(postgres_store.engine) as session:
        events = session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.task_id == task.id)
            .order_by(OutboxEvent.task_revision)
        ).all()
        assert len(events) == 3
        assert all(item.delivery_state == "pending" for item in events)

    try:
        timeline.page("intruder", task.id)
    except StoreError as error:
        assert error.status_code == 404
    else:
        raise AssertionError("Foreign owner accessed task timeline")
