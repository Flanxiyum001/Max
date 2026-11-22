"""MAX PR sandbox dispatcher: ephemeral GitHub Actions, never Render shell.

The MAX owner approves each workflow dispatch. The target PR is pinned to its
current head SHA and the workflow runs in a disposable, read-only GitHub-hosted
runner. This module never merges a PR, pushes code, or runs PR code on Render.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from max_core.web.github_connect import WebGitHubConnector

_REPOSITORY = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})\Z")
_SHA = re.compile(r"[a-f0-9]{40}\Z")
_PR = re.compile(r"[1-9][0-9]{0,5}\Z")
_REVIEW = re.compile(
    r"\b(?:review|audit|validate|test|check|sandbox|inspect)\b"
    r"[\s\S]{0,90}?\b(?:pr|pull\s+request)\s*#?\s*([1-9][0-9]{0,5})\b",
    re.I,
)
_REPO_REFERENCE = re.compile(
    r"\b(?:in|on|for)\s+([A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100})\b",
    re.I,
)
_WORKFLOW = "max-pr-sandbox.yml"
_DEFAULT_REPO = "Flanxiyum001/Max"
_API = "https://api.github.com"
_MAX_RESPONSE = 40_000
_COOLDOWN_SECONDS = 300


class SandboxError(ValueError):
    """Fixed, user-readable error codes only; never include provider responses."""


@dataclass(frozen=True)
class SandboxTarget:
    repository: str
    pr_number: int
    head_sha: str
    default_branch: str
    connection_generation: int


@dataclass(frozen=True)
class SandboxRun:
    repository: str
    pr_number: int
    head_sha: str
    run_id: int | None
    run_url: str | None


def parse_sandbox_request(text: str) -> tuple[str, int] | None:
    """Parse an explicit user PR review request. No model-generated tool call."""
    bounded = text.strip()[:220]
    match = _REVIEW.search(bounded)
    if match is None:
        return None
    pr = int(match.group(1))
    repo = _REPO_REFERENCE.search(bounded)
    return (repo.group(1) if repo else _DEFAULT_REPO, pr)


class GitHubPRSandbox:
    """Runs only pre-installed, read-only workflows in owner-owned repositories."""

    def __init__(
        self,
        github: WebGitHubConnector,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._github = github
        self._transport = transport
        self._submitted: dict[tuple[str, int, str], float] = {}

    def _identity(self) -> tuple[str, str, int]:
        with self._github._lock:
            token = self._github._token
            owner = self._github._account
            generation = self._github._generation
        if not token or not owner:
            raise SandboxError("sandbox_github_not_connected")
        return token, owner, generation

    async def _api(
        self, token: str, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not path.startswith("/repos/") or "?" in path or ".." in path:
            raise SandboxError("sandbox_api_path_invalid")
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                base_url=_API,
                timeout=12.0,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    json=body,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Accept-Encoding": "identity",
                        "Authorization": "Bearer " + token,
                        "X-GitHub-Api-Version": "2026-03-10",
                    },
                )
            if response.status_code == 404:
                raise SandboxError("sandbox_not_installed_or_pr_not_found")
            if response.status_code not in (200, 201, 204):
                raise SandboxError("sandbox_github_api_failed")
            if len(response.content) > _MAX_RESPONSE:
                raise SandboxError("sandbox_github_response_too_large")
            if response.status_code == 204:
                return {}
            result = response.json()
            if not isinstance(result, dict):
                raise SandboxError("sandbox_github_invalid_response")
            return result
        except SandboxError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            raise SandboxError("sandbox_github_api_failed") from None

    async def _inspect(
        self, repository: str, pr_number: int, token: str, account: str, generation: int
    ) -> SandboxTarget:
        if not _REPOSITORY.fullmatch(repository) or not _PR.fullmatch(str(pr_number)):
            raise SandboxError("sandbox_invalid_pr")
        owner = repository.split("/", 1)[0]
        # V1 deliberately supports the authorized owner's repositories only.
        if owner.lower() != account.lower():
            raise SandboxError("sandbox_repo_owner_not_authorized")
        pr = await self._api(token, "GET", f"/repos/{repository}/pulls/{pr_number}")
        head = pr.get("head")
        base = pr.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise SandboxError("sandbox_invalid_pr_response")
        head_repo = head.get("repo")
        base_repo = base.get("repo")
        if (
            pr.get("state") != "open"
            or not isinstance(head_repo, dict)
            or not isinstance(base_repo, dict)
            or head_repo.get("full_name", "").lower() != repository.lower()
            or base_repo.get("full_name", "").lower() != repository.lower()
        ):
            raise SandboxError("sandbox_only_open_same_repo_prs")
        sha = head.get("sha")
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise SandboxError("sandbox_invalid_head_sha")
        default_branch = base_repo.get("default_branch")
        if not isinstance(default_branch, str) or not re.fullmatch(
            r"[A-Za-z0-9_.\-/]{1,150}", default_branch
        ):
            raise SandboxError("sandbox_default_branch_invalid")
        workflow = await self._api(
            token, "GET", f"/repos/{repository}/actions/workflows/{_WORKFLOW}"
        )
        if workflow.get("state") != "active":
            raise SandboxError("sandbox_workflow_inactive")
        return SandboxTarget(repository, pr_number, sha, default_branch, generation)

    def preview(self, repository: str, pr_number: int) -> SandboxTarget:
        token, owner, generation = self._identity()
        try:
            return asyncio.run(self._inspect(repository, pr_number, token, owner, generation))
        except SandboxError:
            raise
        except Exception:
            raise SandboxError("sandbox_preview_failed") from None

    def dispatch(self, target: SandboxTarget) -> SandboxRun:
        token, owner, generation = self._identity()
        if generation != target.connection_generation:
            raise SandboxError("sandbox_connection_changed")
        refreshed = asyncio.run(
            self._inspect(target.repository, target.pr_number, token, owner, generation)
        )
        if refreshed != target:
            raise SandboxError("sandbox_pr_changed_retry")
        key = (target.repository.lower(), target.pr_number, target.head_sha)
        if time.monotonic() - self._submitted.get(key, float("-inf")) < _COOLDOWN_SECONDS:
            raise SandboxError("sandbox_duplicate_cooldown")
        with self._github._lock:
            if (
                self._github._generation != generation
                or self._github._token != token
                or self._github._account != owner
            ):
                raise SandboxError("sandbox_connection_changed")
        payload = {
            "ref": target.default_branch,
            "return_run_details": True,
            "inputs": {
                "pr_number": str(target.pr_number),
                "expected_sha": target.head_sha,
            },
        }
        response = asyncio.run(
            self._api(
                token,
                "POST",
                f"/repos/{target.repository}/actions/workflows/{_WORKFLOW}/dispatches",
                payload,
            )
        )
        self._submitted[key] = time.monotonic()
        run_id = response.get("workflow_run_id")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            run_id = None
        # Locally constructed from known repository slug and numeric run ID.
        run_url = (
            f"https://github.com/{target.repository}/actions/runs/{run_id}"
            if run_id is not None
            else None
        )
        return SandboxRun(target.repository, target.pr_number, target.head_sha, run_id, run_url)

    def status(self, run: SandboxRun) -> dict[str, str]:
        token, owner, _generation = self._identity()
        if run.repository.split("/", 1)[0].lower() != owner.lower():
            raise SandboxError("sandbox_repo_owner_not_authorized")
        if run.run_id is None:
            return {"state": "queued", "conclusion": "unknown"}
        result = asyncio.run(
            self._api(token, "GET", f"/repos/{run.repository}/actions/runs/{run.run_id}")
        )
        state = result.get("status")
        conclusion = result.get("conclusion")
        if state not in {"queued", "in_progress", "completed", "waiting", "requested", "pending"}:
            raise SandboxError("sandbox_invalid_run_status")
        if conclusion not in (
            None,
            "success",
            "failure",
            "cancelled",
            "skipped",
            "timed_out",
            "action_required",
            "neutral",
        ):
            raise SandboxError("sandbox_invalid_run_status")
        return {"state": state, "conclusion": conclusion or "pending"}
