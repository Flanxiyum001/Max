"""Local, read-only CLI for task state and bounded metadata-event timeline.

Usage: python -m max_core.timeline_cli <task-uuid> [--limit 20] [--max-pages 5]
Requires MAX_API_BEARER_TOKEN in the environment; never accepts a token argument.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from max_core.config import ConfigurationError, Settings

_EVENT_NAME = re.compile(r"^[a-z0-9._-]{1,64}$")
_KNOWN_STATES = {
    "queued",
    "running",
    "cancelling",
    "cancelled",
    "awaiting_verification",
    "completed",
    "needs_reconciliation",
}


class TimelineClientError(Exception):
    """Public CLI failures exclude secrets, raw server responses and URLs."""


def _local_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise TimelineClientError("Invalid loopback server URL") from exc
    if (
        parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or port is None
        or not 1024 <= port <= 65535
    ):
        raise TimelineClientError("Only http://127.0.0.1:<port> is supported")
    return f"http://127.0.0.1:{port}"


def _json(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200:
        raise TimelineClientError(f"Local MAX request failed (HTTP {response.status_code})")
    try:
        result = response.json()
    except ValueError as exc:
        raise TimelineClientError("Invalid response from local MAX") from exc
    if not isinstance(result, dict):
        raise TimelineClientError("Invalid response from local MAX")
    return cast(dict[str, Any], result)


def render_timeline(
    client: httpx.Client,
    task_id: UUID,
    *,
    limit: int = 20,
    max_pages: int = 5,
) -> str:
    """Read-only; sanitize all response fields before adding them to terminal output."""
    if not 1 <= limit <= 100 or not 1 <= max_pages <= 20:
        raise TimelineClientError("Invalid local pagination limits")
    path = f"/v1/tasks/{task_id}"
    task = _json(client.get(path))
    state, revision = task.get("state"), task.get("revision")
    if (
        task.get("id") != str(task_id)
        or not isinstance(state, str)
        or state not in _KNOWN_STATES
        or type(revision) is not int
        or revision < 1
    ):
        raise TimelineClientError("Invalid task status response")

    lines = [f"Task {task_id}: state={state}, revision={revision}"]
    seen_cursors: set[str] = set()
    cursor: str | None = None
    for _ in range(max_pages):
        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        page = _json(client.get(f"{path}/events", params=params))
        events = page.get("events")
        if (
            page.get("task_id") != str(task_id)
            or type(page.get("snapshot_revision")) is not int
            or not isinstance(events, list)
            or len(events) > limit
        ):
            raise TimelineClientError("Invalid event timeline response")
        for item in events:
            if not isinstance(item, dict):
                raise TimelineClientError("Invalid event timeline response")
            kind = item.get("kind")
            event_rev = item.get("task_revision")
            raw_time = item.get("created_at")
            if (
                not isinstance(kind, str)
                or not _EVENT_NAME.fullmatch(kind)
                or type(event_rev) is not int
                or event_rev < 1
                or not isinstance(raw_time, str)
            ):
                raise TimelineClientError("Invalid event timeline response")
            try:
                timestamp = datetime.fromisoformat(raw_time).isoformat()
            except ValueError as exc:
                raise TimelineClientError("Invalid event timeline response") from exc
            lines.append(f"  r{event_rev}: {kind} ({timestamp})")
        next_value = page.get("next_cursor")
        if next_value is None:
            break
        if (
            not isinstance(next_value, str)
            or not 20 <= len(next_value) <= 512
            or next_value in seen_cursors
        ):
            raise TimelineClientError("Invalid event timeline cursor")
        seen_cursors.add(next_value)
        cursor = next_value
    else:
        lines.append("  (additional events not displayed; increase --max-pages to read more)")
    if len(lines) == 1:
        lines.append("  (no metadata events recorded)")
    lines.append("Note: metadata events are not evidence that an external action happened.")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read local MAX task history (no execution)")
    parser.add_argument("task_id", type=UUID, help="Task UUID")
    parser.add_argument("--limit", type=int, default=20, help="Events per page (1-100)")
    parser.add_argument("--max-pages", type=int, default=5, help="Maximum pages to read (1-20)")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765", help="Loopback MAX URL")
    args = parser.parse_args(argv)
    try:
        url = _local_url(args.base_url)
        token = Settings.from_env().bearer_token
        with httpx.Client(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            follow_redirects=False,
            timeout=5.0,
        ) as client:
            output = render_timeline(
                client, args.task_id, limit=args.limit, max_pages=args.max_pages
            )
    except (ConfigurationError, TimelineClientError) as exc:
        print(f"MAX timeline: {exc}", file=sys.stderr)
        return 2
    except httpx.HTTPError:
        print(
            "MAX timeline: Local MAX is unavailable or returned an unsafe response", file=sys.stderr
        )
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
