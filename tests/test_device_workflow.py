"""Negative-first tests for PC workflow policy; no device or network used."""

from __future__ import annotations

import pytest

from max_core.development_team import CheckEvidence, CheckStatus
from max_core.device_workflow import (
    DeviceWorkflowError,
    Heartbeat,
    HeartbeatRegistry,
    IsolationEvidence,
    PCWorkflow,
    Phase,
    magic_packet,
    sign_heartbeat,
)

KEY = b"x" * 32
DEVICE = "gaming-pc"
BOOT = "a" * 32
NONCE = "b" * 32
HEAD = "c" * 40
CHECKS = ("lint", "format", "typing", "unit_security", "postgres", "dependency_audit")


def heartbeat(**overrides: object) -> Heartbeat:
    data = dict(
        device_id=DEVICE,
        boot_id=BOOT,
        nonce=NONCE,
        sent_at_ms=1_000_000,
        active_jobs=0,
        user_active=False,
        unsaved_work=False,
        suspend_supported=True,
    )
    data.update(overrides)
    return Heartbeat(**data)


def registry() -> HeartbeatRegistry:
    return HeartbeatRegistry({DEVICE: KEY})


def record(r: HeartbeatRegistry, hb: Heartbeat | None = None, *, now: int = 1_000_000) -> None:
    message = hb if hb is not None else heartbeat()
    r.receive(message, sign_heartbeat(message, KEY), now_ms=now)


def checks(revision: str = HEAD) -> tuple[CheckEvidence, ...]:
    return tuple(
        CheckEvidence(name, revision, CheckStatus.PASSED, f"check-{name}") for name in CHECKS
    )


def test_wol_magic_packet_exact_payload_no_network() -> None:
    packet = magic_packet("02:11:22:33:44:55")
    assert len(packet) == 102
    assert packet == b"\xff" * 6 + bytes.fromhex("021122334455") * 16
    assert packet == magic_packet("021122334455")
    assert packet == magic_packet("02-11-22-33-44-55")


@pytest.mark.parametrize(
    "mac",
    [
        "",
        "aa-bb-cc",
        "01:00:5e:00:00:01",
        "ff:ff:ff:ff:ff:ff",
        "00:00:00:00:00:00",
        "02:11-22:33:44:55",
        "02:11:22:33:44:55\n",
    ],
)
def test_invalid_wol_targets_denied(mac: str) -> None:
    with pytest.raises(DeviceWorkflowError, match="invalid_mac"):
        magic_packet(mac)


def test_authenticated_heartbeat_recorded_and_replay_denied() -> None:
    r = registry()
    hb = heartbeat()
    with pytest.raises(DeviceWorkflowError, match="unauthorized_heartbeat"):
        r.receive(hb, "0" * 64, now_ms=1_000_000)
    record(r, hb)
    assert r.current(DEVICE, now_ms=1_000_010) is not None
    assert r.safe_to_suspend(DEVICE, now_ms=1_000_010)
    with pytest.raises(DeviceWorkflowError):
        record(r, hb)
    assert r.current(DEVICE, now_ms=1_100_001) is None
    assert not r.safe_to_suspend(DEVICE, now_ms=1_100_001)


@pytest.mark.parametrize(
    "changes",
    [
        {"unsaved_work": True},
        {"user_active": True},
        {"active_jobs": 1},
        {"suspend_supported": False},
    ],
)
def test_heartbeat_cannot_authorize_unsafe_sleep(changes: dict[str, object]) -> None:
    r = registry()
    record(r, heartbeat(**changes))
    assert not r.safe_to_suspend(DEVICE, now_ms=1_000_100)


def test_heartbeat_scopes_freshness_and_enrolment() -> None:
    r = registry()
    stale = heartbeat(sent_at_ms=900_000)
    with pytest.raises(DeviceWorkflowError, match="stale_heartbeat"):
        record(r, stale)
    rogue = heartbeat(device_id="rogue")
    with pytest.raises(DeviceWorkflowError, match="unauthorized_heartbeat"):
        r.receive(rogue, sign_heartbeat(rogue, KEY), now_ms=1_000_000)
    with pytest.raises(DeviceWorkflowError, match="invalid_device_key"):
        sign_heartbeat(heartbeat(), b"short")
    with pytest.raises(DeviceWorkflowError, match="invalid_heartbeat"):
        heartbeat(active_jobs=True)


def test_approved_workflow_reaches_verified_report_only_at_exact_head() -> None:
    r = registry()
    w = PCWorkflow("task-1", DEVICE, "Flanxiyum001/Max")
    with pytest.raises(DeviceWorkflowError, match="owner_approval_required"):
        w.request_wake(owner_authorized=False)
    w.request_wake(owner_authorized=True)
    with pytest.raises(DeviceWorkflowError, match="device_not_verified_online"):
        w.observe_online(r, now_ms=1_000_000)
    record(r)
    w.observe_online(r, now_ms=1_000_000)
    with pytest.raises(DeviceWorkflowError, match="isolation_not_verified"):
        w.observe_isolation(IsolationEvidence(False, True, True))
    w.observe_isolation(IsolationEvidence(True, True, True))
    w.draft_created("https://github.com/Flanxiyum001/Max/pull/123", HEAD)
    with pytest.raises(DeviceWorkflowError, match="ci_not_verified"):
        w.verify_ci(checks("d" * 40))
    with pytest.raises(DeviceWorkflowError, match="ci_not_verified"):
        w.verify_ci(checks()[:-1])
    w.verify_ci(checks())
    w.report()
    assert w.phase == Phase.REPORTED
    assert w.pr_url == "https://github.com/Flanxiyum001/Max/pull/123"
    assert w.head_sha == HEAD
    w.permit_suspend(r, now_ms=1_000_100, owner_authorized=True)
    assert w.phase == Phase.SUSPEND_ELIGIBLE
    w.finish()
    assert w.phase == Phase.FINISHED
    with pytest.raises(DeviceWorkflowError, match="invalid_workflow_transition"):
        w.request_wake(owner_authorized=True)


def test_no_sleep_without_fresh_idle_heartbeat_and_separate_consent() -> None:
    r = registry()
    record(r, heartbeat(user_active=True))
    w = PCWorkflow("task-2", DEVICE, "Flanxiyum001/Max")
    w.request_wake(owner_authorized=True)
    w.observe_online(r, now_ms=1_000_000)
    w.observe_isolation(IsolationEvidence(True, True, True))
    w.draft_created("https://github.com/Flanxiyum001/Max/pull/9", HEAD)
    w.verify_ci(checks())
    w.report()
    with pytest.raises(DeviceWorkflowError, match="suspend_not_authorized_or_safe"):
        w.permit_suspend(r, now_ms=1_000_000, owner_authorized=True)
    with pytest.raises(DeviceWorkflowError, match="suspend_not_authorized_or_safe"):
        w.permit_suspend(r, now_ms=1_000_000, owner_authorized=False)
    w.finish()  # reporting may finish without suspending the user's PC


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/Flanxiyum001/Max/pull/1",
        "https://github.com/Flanxiyum001/other/pull/1",
        "https://github.com.evil.test/Flanxiyum001/Max/pull/1",
        "https://github.com/Flanxiyum001/Max/pull/1?token=abc",
        "https://github.com/Flanxiyum001/Max/pull/0",
    ],
)
def test_mismatched_pr_urls_rejected(url: str) -> None:
    w = PCWorkflow("task-3", DEVICE, "Flanxiyum001/Max")
    w.request_wake(owner_authorized=True)
    r = registry()
    record(r)
    w.observe_online(r, now_ms=1_000_000)
    w.observe_isolation(IsolationEvidence(True, True, True))
    with pytest.raises(DeviceWorkflowError, match="invalid_pr_evidence"):
        w.draft_created(url, HEAD)


def test_blocked_workflow_never_claims_review_or_sleep() -> None:
    w = PCWorkflow("task-4", DEVICE, "Flanxiyum001/Max")
    w.block()
    assert w.phase == Phase.BLOCKED
    with pytest.raises(DeviceWorkflowError):
        w.finish()
