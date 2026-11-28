"""Keep MAX's state in the private Max-memory GitHub repo.

The web orb runs on a free host whose disk is wiped on every restart. This
syncs MAX's small state files (memory, reminders, notes, jobs, contacts,
lessons) to ``state/web/`` in the owner's private repo:

* at startup, files in the repo are downloaded into the data dir (repo wins);
* while running, a background thread uploads any file that changed;
* the owner-approved ``MEMORY.md`` facts are imported into memory on first run.

Needs a fine-grained GitHub token with *Contents: read and write* on that one
repo (``MAX_MEMORY_GITHUB_TOKEN``) and ``MAX_MEMORY_REPO`` (owner/name).
The token is only sent to api.github.com.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

API = "https://api.github.com"
PREFIX = "state/web"
PATTERNS = ("*.json", "jobs.sqlite3", "comms.sqlite3", "evolution.sqlite3")
MAX_FILE_BYTES = 900_000  # GitHub contents API limit is 1 MB


class MemoryRepoSync:
    def __init__(
        self,
        repo: str,
        token: str,
        data_dir: Path,
        *,
        branch: str = "main",
        http: Callable[[], httpx.Client] | None = None,
    ) -> None:
        if repo.count("/") != 1:
            raise ValueError("invalid_memory_repo")
        self.repo, self.branch, self.dir = repo, branch, Path(data_dir)
        self._token = token
        self._http = http or (lambda: httpx.Client(timeout=20.0, follow_redirects=False))
        self._shas: dict[str, str] = {}  # repo path -> blob sha
        self._digests: dict[str, str] = {}  # local name -> content digest last synced
        self._stop = threading.Event()
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env: Mapping[str, str], data_dir: Path) -> MemoryRepoSync | None:
        token = env.get("MAX_MEMORY_GITHUB_TOKEN", "").strip()
        repo = env.get("MAX_MEMORY_REPO", "").strip()
        if not token or not repo:
            return None
        return cls(repo, token, data_dir, branch=env.get("MAX_MEMORY_BRANCH", "main").strip())

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get(self, client: httpx.Client, path: str) -> Any:
        response = client.get(
            f"{API}/repos/{self.repo}/contents/{path}",
            params={"ref": self.branch},
            headers=self._headers(),
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _local_files(self) -> list[Path]:
        found: set[Path] = set()
        for pattern in PATTERNS:
            found.update(p for p in self.dir.glob(pattern) if p.is_file())
        return sorted(found)

    # download ----------------------------------------------------------------
    def pull(self) -> int:
        """Download repo state into the data dir. Returns files written."""
        self.dir.mkdir(parents=True, exist_ok=True)
        written = 0
        with self._lock, self._http() as client:
            listing = self._get(client, PREFIX) or []
            for entry in listing if isinstance(listing, list) else []:
                name = str(entry.get("name", ""))
                if entry.get("type") != "file" or "/" in name or name.startswith("."):
                    continue
                if not any(Path(name).match(p) for p in PATTERNS):
                    continue
                item = self._get(client, f"{PREFIX}/{name}")
                if not item or item.get("encoding") != "base64":
                    continue
                data = base64.b64decode(item["content"])
                (self.dir / name).write_bytes(data)
                self._shas[f"{PREFIX}/{name}"] = str(item["sha"])
                self._digests[name] = self._digest(data)
                written += 1
            if not (self.dir / "memory.json").exists():
                written += self._import_memory_md(client)
        return written

    def _import_memory_md(self, client: httpx.Client) -> int:
        item = self._get(client, "MEMORY.md")
        if not item or item.get("encoding") != "base64":
            return 0
        text = base64.b64decode(item["content"]).decode("utf-8", "replace")
        facts = [
            line[2:].strip()
            for line in text.splitlines()
            if line.startswith("- ") and line[2:].strip()
        ][:500]
        if not facts:
            return 0
        rows = [
            {"id": hashlib.sha1(f.encode()).hexdigest()[:8], "fact": f, "created": ""}  # noqa: S324
            for f in facts
        ]
        (self.dir / "memory.json").write_text(
            json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        return 1

    # upload ------------------------------------------------------------------
    def push(self) -> int:
        """Upload every state file that changed since the last sync."""
        uploaded = 0
        with self._lock, self._http() as client:
            for path in self._local_files():
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                digest = self._digest(data)
                if len(data) > MAX_FILE_BYTES or self._digests.get(path.name) == digest:
                    continue
                repo_path = f"{PREFIX}/{path.name}"
                if repo_path not in self._shas:
                    existing = self._get(client, repo_path)
                    if existing:
                        self._shas[repo_path] = str(existing["sha"])
                body: dict[str, Any] = {
                    "message": f"MAX state: {path.name}",
                    "content": base64.b64encode(data).decode(),
                    "branch": self.branch,
                }
                if repo_path in self._shas:
                    body["sha"] = self._shas[repo_path]
                response = client.put(
                    f"{API}/repos/{self.repo}/contents/{repo_path}",
                    json=body,
                    headers=self._headers(),
                )
                if response.status_code == 409:  # stale sha: refresh next round
                    self._shas.pop(repo_path, None)
                    continue
                response.raise_for_status()
                self._shas[repo_path] = str(response.json()["content"]["sha"])
                self._digests[path.name] = digest
                uploaded += 1
        return uploaded

    def start(self, interval: float = 60.0) -> None:
        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.push()
                except Exception as exc:
                    log.warning("memory repo sync failed: %s", type(exc).__name__)

        threading.Thread(target=loop, name="max-memory-sync", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.push()
        except Exception as exc:
            log.warning("final memory repo sync failed: %s", type(exc).__name__)
