"""PR sandbox: trusted dispatch, pinned SHA, owner approval and no merge."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from max_core.web.app import create_app
from max_core.web.github_connect import WebGitHubConnector
from max_core.web.pr_sandbox import (
    GitHubPRSandbox,
    SandboxError,
    SandboxRun,
    SandboxTarget,
    parse_sandbox_request,
)

REPO = "Flanxiyum001/Max"
HEAD = "a" * 40
CHANGED = "b" * 40
ENV = {"MAX_WEB_PASSCODE": "very long passcode", "MAX_WEB_INSECURE_COOKIE": "yes"}


def connected_github() -> WebGitHubConnector:
    github = WebGitHubConnector(ENV)
    with github._lock:
        github._token = "test_auth_token_never_expose"
        github._account = "Flanxiyum001"
        github._generation = 1
    return github


def fake_transport(
    state: dict[str, Any],
    requested: list[tuple[str, str]],
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requested.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer test_auth_token_never_expose"
        if request.url.path.endswith("/pulls/42"):
            return httpx.Response(
                200,
                json={
                    "state": state.get("status", "open"),
                    "head": {
                        "repo": {"full_name": state.get("head_repo", REPO)},
                        "sha": state.get("head", HEAD),
                    },
                    "base": {"repo": {"full_name": REPO, "default_branch": "max"}},
                },
            )
        if request.url.path.endswith("/workflows/max-pr-sandbox.yml"):
            return httpx.Response(200, json={"state": "active"})
        if request.url.path.endswith("/workflows/max-pr-sandbox.yml/dispatches"):
            data = __import__("json").loads(request.content)
            assert data == {
                "ref": "max",
                "return_run_details": True,
                "inputs": {"pr_number": "42", "expected_sha": HEAD},
            }
            return httpx.Response(200, json={"workflow_run_id": 93744})
        if request.url.path.endswith("/actions/runs/93744"):
            return httpx.Response(200, json={"status": "completed", "conclusion": "success"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_parse_owner_request_not_random_pr_text() -> None:
    assert parse_sandbox_request("MAX, review PR #42") == (REPO, 42)
    assert parse_sandbox_request("Can you test PR 42 in Flanxiyum001/Ryoko?") == (
        "Flanxiyum001/Ryoko",
        42,
    )
    assert parse_sandbox_request("I see PR #42 is open") is None
    assert parse_sandbox_request("merge PR #42") is None
    assert parse_sandbox_request("remove all PRs") is None


def test_dispatch_pins_sha_and_never_automatically_merges() -> None:
    github = connected_github()
    state: dict[str, Any] = {}
    requested: list[tuple[str, str]] = []
    sandbox = GitHubPRSandbox(github, transport=fake_transport(state, requested))
    preview = sandbox.preview(REPO, 42)
    assert preview == SandboxTarget(REPO, 42, HEAD, "max", 1)
    run = sandbox.dispatch(preview)
    assert run == SandboxRun(
        REPO, 42, HEAD, 93744, "https://github.com/Flanxiyum001/Max/actions/runs/93744"
    )
    assert sandbox.status(run) == {"state": "completed", "conclusion": "success"}
    assert all("/merges" not in path for _, path in requested)
    assert all("/git/refs" not in path for _, path in requested)
    assert [method for method, _ in requested].count("POST") == 1
    with pytest.raises(SandboxError, match="sandbox_duplicate_cooldown"):
        sandbox.dispatch(preview)


def test_changed_sha_stops_dispatch_and_consumes_no_runner_minutes() -> None:
    state: dict[str, Any] = {}
    requested: list[tuple[str, str]] = []
    sandbox = GitHubPRSandbox(connected_github(), transport=fake_transport(state, requested))
    preview = sandbox.preview(REPO, 42)
    state["head"] = CHANGED
    with pytest.raises(SandboxError, match="sandbox_pr_changed_retry"):
        sandbox.dispatch(preview)
    assert not any(method == "POST" for method, _ in requested)


def test_fork_or_closed_pr_and_unauthorized_owner_denied() -> None:
    state: dict[str, Any] = {"head_repo": "attacker/Max"}
    requested: list[tuple[str, str]] = []
    sandbox = GitHubPRSandbox(connected_github(), transport=fake_transport(state, requested))
    with pytest.raises(SandboxError, match="sandbox_only_open_same_repo_prs"):
        sandbox.preview(REPO, 42)
    with pytest.raises(SandboxError, match="sandbox_repo_owner_not_authorized"):
        sandbox.preview("other/Project", 42)
    assert not any(method == "POST" for method, _ in requested)
    state["head_repo"] = REPO
    state["status"] = "closed"
    with pytest.raises(SandboxError, match="sandbox_only_open_same_repo_prs"):
        sandbox.preview(REPO, 42)


def test_disconnect_invalidates_preview() -> None:
    github = connected_github()
    state: dict[str, Any] = {}
    requested: list[tuple[str, str]] = []
    sandbox = GitHubPRSandbox(github, transport=fake_transport(state, requested))
    preview = sandbox.preview(REPO, 42)
    github.disconnect()
    with pytest.raises(SandboxError, match="sandbox_github_not_connected"):
        sandbox.dispatch(preview)
    assert not any(method == "POST" for method, _ in requested)


def test_web_ask_requires_specific_owner_approval() -> None:
    github = connected_github()

    class MockSandbox:
        calls = 0

        def preview(self, repo: str, number: int) -> SandboxTarget:
            assert repo == REPO and number == 42
            return SandboxTarget(repo, number, HEAD, "max", 1)

        def dispatch(self, target: SandboxTarget) -> SandboxRun:
            self.calls += 1
            return SandboxRun(
                REPO, 42, HEAD, 93744, "https://github.com/Flanxiyum001/Max/actions/runs/93744"
            )

        def status(self, run: SandboxRun) -> dict[str, str]:
            return {"state": "completed", "conclusion": "success"}

    mock = MockSandbox()
    app = create_app(
        ENV,
        github_connector=github,
        sandbox_runner=mock,  # type: ignore[arg-type]
        session_factory=lambda: None,
    )
    client = TestClient(app)
    assert client.post("/api/ask", json={"text": "review PR #42"}).status_code == 401
    assert client.post("/api/login", json={"passcode": ENV["MAX_WEB_PASSCODE"]}).status_code == 200
    created = client.post("/api/ask", json={"text": "MAX review PR #42"})
    assert created.status_code == 200
    job = created.json()["job"]
    waiting = client.get(f"/api/job/{job}").json()
    assert waiting["status"] == "approval"
    assert waiting["approval"]["tool"] == "sandbox.run_pr"
    assert waiting["approval"]["arguments"]["pinned_sha"] == HEAD
    assert mock.calls == 0
    assert client.post("/api/ask", json={"text": "other"}).status_code == 409
    assert client.post(f"/api/job/{job}/approve", json={"allow": True}).status_code == 200
    for _ in range(100):
        answer = client.get(f"/api/job/{job}").json()
        if answer["status"] == "done":
            break
        time.sleep(0.01)
    assert answer["status"] == "done"
    assert mock.calls == 1
    assert "NOT a merge approval" in answer["answer"]
    assert answer["sandbox"]["run_id"] == 93744
    assert client.get(f"/api/sandbox/run/{job}").json()["conclusion"] == "success"
    assert client.post(f"/api/job/{job}/approve", json={"allow": True}).status_code == 409


def test_web_denied_approval_does_not_dispatch() -> None:
    github = connected_github()

    class DeniedSandbox:
        calls = 0

        def preview(self, repo: str, number: int) -> SandboxTarget:
            return SandboxTarget(repo, number, HEAD, "max", 1)

        def dispatch(self, target: SandboxTarget) -> SandboxRun:
            self.calls += 1
            raise AssertionError("must not dispatch")

    sandbox = DeniedSandbox()
    client = TestClient(
        create_app(
            ENV,
            github_connector=github,
            sandbox_runner=sandbox,  # type: ignore[arg-type]
            session_factory=lambda: None,
        )
    )
    client.post("/api/login", json={"passcode": ENV["MAX_WEB_PASSCODE"]})
    job = client.post("/api/ask", json={"text": "audit PR 42"}).json()["job"]
    assert client.post(f"/api/job/{job}/approve", json={"allow": False}).status_code == 200
    for _ in range(100):
        result = client.get(f"/api/job/{job}").json()
        if result["status"] == "done":
            break
        time.sleep(0.01)
    assert result["status"] == "done" and "cancelled" in result["answer"]
    assert sandbox.calls == 0


def test_workflow_is_non_privileged_and_does_not_auto_merge() -> None:
    text = (
        Path(__file__).resolve().parents[1] / ".github/workflows/max-pr-sandbox.yml"
    ).read_text()
    assert "workflow_dispatch" in text
    assert "contents: read" in text
    assert "pull-requests: read" in text
    assert "persist-credentials: false" in text
    assert "expected_sha" in text
    assert "pull_request_target" not in text
    assert "secrets." not in text
    assert "gh pr merge" not in text.lower()
    assert "git push " not in text.lower()
    assert "write-all" not in text.lower()
