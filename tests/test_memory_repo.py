from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx

from max_core.memory_repo import MemoryRepoSync


class FakeGitHub:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.puts: list[str] = []
        self.auth: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.auth.add(request.headers.get("authorization", ""))
        assert request.url.host == "api.github.com"
        path = request.url.path.split("/contents/", 1)[1]
        if request.method == "GET":
            if path in self.files:
                data = self.files[path]
                return httpx.Response(
                    200,
                    json={
                        "type": "file",
                        "encoding": "base64",
                        "sha": f"sha-{len(data)}",
                        "content": base64.b64encode(data).decode(),
                        "name": path.rsplit("/", 1)[-1],
                    },
                )
            children = [k for k in self.files if k.startswith(path + "/")]
            if children:
                return httpx.Response(
                    200, json=[{"type": "file", "name": k.rsplit("/", 1)[-1]} for k in children]
                )
            return httpx.Response(404, json={})
        body: Any = json.loads(request.content)
        self.files[path] = base64.b64decode(body["content"])
        self.puts.append(path)
        return httpx.Response(200, json={"content": {"sha": f"new-{len(self.puts)}"}})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def test_first_run_imports_memory_md_then_pushes_changes(tmp_path: Path) -> None:
    gh = FakeGitHub(
        {"MEMORY.md": b"# MAX memory\n\n- Owner prefers concise replies.\n- Lives in India.\n"}
    )
    sync = MemoryRepoSync("o/Max-memory", "tok", tmp_path, http=gh.client)
    assert sync.pull() == 1
    facts = [row["fact"] for row in json.loads((tmp_path / "memory.json").read_text())]
    assert facts == ["Owner prefers concise replies.", "Lives in India."]
    assert sync.push() == 1 and gh.puts == ["state/web/memory.json"]
    assert sync.push() == 0  # unchanged
    (tmp_path / "reminders.json").write_text("[]")
    assert sync.push() == 1 and gh.auth == {"Bearer tok"}


def test_restart_restores_state_from_repo(tmp_path: Path) -> None:
    gh = FakeGitHub(
        {
            "state/web/memory.json": b'[{"id":"a","fact":"x","created":""}]',
            "state/web/jobs.sqlite3": b"SQLite",
            "state/web/evil.sh": b"rm -rf",
        }
    )
    sync = MemoryRepoSync("o/Max-memory", "tok", tmp_path, http=gh.client)
    assert sync.pull() == 2
    assert (tmp_path / "jobs.sqlite3").read_bytes() == b"SQLite"
    assert not (tmp_path / "evil.sh").exists()
    assert sync.push() == 0  # nothing changed since restore


def test_from_env_needs_token_and_repo(tmp_path: Path) -> None:
    assert MemoryRepoSync.from_env({}, tmp_path) is None
    assert (
        MemoryRepoSync.from_env(
            {"MAX_MEMORY_GITHUB_TOKEN": "t", "MAX_MEMORY_REPO": "o/r"}, tmp_path
        )
        is not None
    )
