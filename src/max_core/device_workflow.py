"""Fail-closed device workflow contracts; no host, network or power side effects.

Only authenticated host adapters may supply heartbeats, isolation evidence and CI
observations. A model must never manufacture these values or bypass MAX Trust.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

from max_core.development_team import CheckEvidence, CheckStatus

_DEVICE: Final = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_NONCE: Final = re.compile(r"^[0-9a-f]{32}$")
_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
_REPO: Final = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REQUIRED: Final = frozenset(
    {"lint", "format", "typing", "unit_security", "postgres", "dependency_audit"}
)


class DeviceWorkflowError(ValueError):
    """Sanitized error: no paths, personal details, credentials or model output."""


def magic_packet(mac: str) -> bytes:
    """Build, but never transmit, a Wake-on-LAN magic packet."""
    if not isinstance(mac, str) or not (
        re.fullmatch(r"[0-9a-fA-F]{12}", mac)
        or re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac)
        or re.fullmatch(r"(?:[0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2}", mac)
    ):
        raise DeviceWorkflowError("invalid_mac")
    address = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if address == bytes(6) or address[0] & 1:
        raise DeviceWorkflowError("invalid_mac")
    return b"\xff" * 6 + address * 16


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """Signed by an enrolled node key outside the model/provider boundary."""

    device_id: str
    boot_id: str
    nonce: str
    sent_at_ms: int
    active_jobs: int
    user_active: bool
    unsaved_work: bool
    suspend_supported: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.device_id, str)
            or not _DEVICE.fullmatch(self.device_id)
            or not isinstance(self.boot_id, str)
            or not _NONCE.fullmatch(self.boot_id)
            or not isinstance(self.nonce, str)
            or not _NONCE.fullmatch(self.nonce)
            or type(self.sent_at_ms) is not int
            or self.sent_at_ms < 0
            or type(self.active_jobs) is not int
            or not 0 <= self.active_jobs <= 100
            or any(
                type(flag) is not bool
                for flag in (self.user_active, self.unsaved_work, self.suspend_supported)
            )
        ):
            raise DeviceWorkflowError("invalid_heartbeat")

    def payload(self) -> bytes:
        return json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")


def sign_heartbeat(heartbeat: Heartbeat, key: bytes) -> str:
    """Node-side helper; deploy the key only in enrolled device secret storage."""
    if not isinstance(key, bytes) or len(key) < 32:
        raise DeviceWorkflowError("invalid_device_key")
    return hmac.new(key, heartbeat.payload(), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class VerifiedHeartbeat:
    data: Heartbeat
    received_at_ms: int


class HeartbeatRegistry:
    """In-memory demo verifier; restart-safe replay storage is a deployment gate.

    The verifier does not create a trusted node key or expose any remote endpoint.
    """

    def __init__(
        self,
        device_keys: dict[str, bytes],
        *,
        skew_ms: int = 30_000,
        ttl_ms: int = 60_000,
    ) -> None:
        if (
            not device_keys
            or any(
                not _DEVICE.fullmatch(device_id)
                or not isinstance(key, bytes)
                or len(key) < 32
                for device_id, key in device_keys.items()
            )
            or type(skew_ms) is not int
            or not 1_000 <= skew_ms <= 120_000
            or type(ttl_ms) is not int
            or not 1_000 <= ttl_ms <= 300_000
        ):
            raise DeviceWorkflowError("invalid_registry")
        self._keys = device_keys.copy()
        self._skew_ms = skew_ms
        self._ttl_ms = ttl_ms
        self._records: dict[str, VerifiedHeartbeat] = {}
        self._nonces: dict[tuple[str, str], int] = {}
        self._last_clock: int | None = None

    def receive(self, heartbeat: Heartbeat, signature: str, *, now_ms: int) -> None:
        if type(now_ms) is not int or now_ms < 0:
            raise DeviceWorkflowError("invalid_clock")
        if self._last_clock is not None and now_ms < self._last_clock:
            raise DeviceWorkflowError("clock_regressed")
        self._last_clock = now_ms
        key = self._keys.get(heartbeat.device_id)
        if (
            key is None
            or not isinstance(signature, str)
            or not re.fullmatch(r"[0-9a-f]{64}", signature)
            or not hmac.compare_digest(sign_heartbeat(heartbeat, key), signature)
        ):
            raise DeviceWorkflowError("unauthorized_heartbeat")
        if abs(heartbeat.sent_at_ms - now_ms) > self._skew_ms:
            raise DeviceWorkflowError("stale_heartbeat")
        previous = self._records.get(heartbeat.device_id)
        if previous is not None and (
            heartbeat.sent_at_ms <= previous.data.sent_at_ms
            and heartbeat.boot_id == previous.data.boot_id
        ):
            raise DeviceWorkflowError("reordered_heartbeat")
        # Do not reuse nonces within the clock-skew acceptance window.
        self._nonces = {
            nonce: received
            for nonce, received in self._nonces.items()
            if now_ms - received <= self._skew_ms * 2
        }
        identity = (heartbeat.device_id, heartbeat.nonce)
        if identity in self._nonces:
            raise DeviceWorkflowError("replayed_heartbeat")
        self._nonces[identity] = now_ms
        self._records[heartbeat.device_id] = VerifiedHeartbeat(heartbeat, now_ms)

    def current(self, device_id: str, *, now_ms: int) -> VerifiedHeartbeat | None:
        if type(now_ms) is not int or now_ms < 0:
            raise DeviceWorkflowError("invalid_clock")
        record = self._records.get(device_id)
        if record is None or not 0 <= now_ms - record.received_at_ms <= self._ttl_ms:
            return None
        return record

    def safe_to_suspend(self, device_id: str, *, now_ms: int) -> bool:
        record = self.current(device_id, now_ms=now_ms)
        return bool(
            record
            and record.data.suspend_supported
            and not record.data.user_active
            and not record.data.unsaved_work
            and record.data.active_jobs == 0
        )


class Phase(StrEnum):
    QUEUED = "queued"
    WAKE_REQUESTED = "wake_requested"
    ONLINE = "online"
    ISOLATED = "isolated"
    DRAFT_PR = "draft_pr"
    CI_VERIFIED = "ci_verified"
    REPORTED = "reported"
    SUSPEND_ELIGIBLE = "suspend_eligible"
    FINISHED = "finished"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class IsolationEvidence:
    """Must come from a trusted local runtime/observer, not an agent's response."""

    rootless_container: bool
    host_secrets_unmounted: bool
    scoped_workspace: bool

    def valid(self) -> bool:
        return all(
            type(flag) is bool and flag
            for flag in (
                self.rootless_container,
                self.host_secrets_unmounted,
                self.scoped_workspace,
            )
        )


@dataclass(slots=True)
class PCWorkflow:
    """Pure transition guard; adapters provide all I/O and authenticated evidence."""

    task_id: str
    device_id: str
    repository: str
    phase: Phase = field(default=Phase.QUEUED, init=False)
    head_sha: str | None = field(default=None, init=False)
    pr_url: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if (
            not _DEVICE.fullmatch(self.task_id)
            or not _DEVICE.fullmatch(self.device_id)
            or not _REPO.fullmatch(self.repository)
            or ".." in self.repository
        ):
            raise DeviceWorkflowError("invalid_workflow_scope")

    def _advance(self, expected: Phase, next_phase: Phase) -> None:
        if self.phase != expected:
            raise DeviceWorkflowError("invalid_workflow_transition")
        self.phase = next_phase

    def request_wake(self, *, owner_authorized: bool) -> None:
        # Authorization must be the independently verified MAX Trust decision.
        if owner_authorized is not True:
            raise DeviceWorkflowError("owner_approval_required")
        self._advance(Phase.QUEUED, Phase.WAKE_REQUESTED)

    def observe_online(self, registry: HeartbeatRegistry, *, now_ms: int) -> None:
        if registry.current(self.device_id, now_ms=now_ms) is None:
            raise DeviceWorkflowError("device_not_verified_online")
        self._advance(Phase.WAKE_REQUESTED, Phase.ONLINE)

    def observe_isolation(self, proof: IsolationEvidence) -> None:
        if not isinstance(proof, IsolationEvidence) or not proof.valid():
            raise DeviceWorkflowError("isolation_not_verified")
        self._advance(Phase.ONLINE, Phase.ISOLATED)

    def draft_created(self, url: str, head_sha: str) -> None:
        if not isinstance(url, str) or not isinstance(head_sha, str):
            raise DeviceWorkflowError("invalid_pr_evidence")
        parsed = urlsplit(url)
        segments = parsed.path.strip("/").split("/")
        expected = self.repository.split("/") + ["pull"]
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or parsed.query
            or parsed.fragment
            or len(segments) != 4
            or segments[:3] != expected
            or not segments[3].isdigit()
            or int(segments[3]) < 1
            or not _SHA.fullmatch(head_sha)
        ):
            raise DeviceWorkflowError("invalid_pr_evidence")
        if self.phase != Phase.ISOLATED:
            raise DeviceWorkflowError("invalid_workflow_transition")
        self.pr_url = url
        self.head_sha = head_sha
        self.phase = Phase.DRAFT_PR

    def verify_ci(self, evidence: tuple[CheckEvidence, ...]) -> None:
        if (
            self.phase != Phase.DRAFT_PR
            or not isinstance(evidence, tuple)
            or len(evidence) > 32
            or not all(isinstance(item, CheckEvidence) for item in evidence)
        ):
            raise DeviceWorkflowError("invalid_ci_evidence")
        names = [item.name for item in evidence]
        if (
            len(names) != len(set(names))
            or not _REQUIRED.issubset(names)
            or any(item.revision != self.head_sha or item.status != CheckStatus.PASSED for item in evidence)
        ):
            raise DeviceWorkflowError("ci_not_verified")
        self.phase = Phase.CI_VERIFIED

    def report(self) -> None:
        self._advance(Phase.CI_VERIFIED, Phase.REPORTED)

    def permit_suspend(
        self,
        registry: HeartbeatRegistry,
        *,
        now_ms: int,
        owner_authorized: bool,
    ) -> None:
        if owner_authorized is not True or not registry.safe_to_suspend(
            self.device_id, now_ms=now_ms
        ):
            raise DeviceWorkflowError("suspend_not_authorized_or_safe")
        self._advance(Phase.REPORTED, Phase.SUSPEND_ELIGIBLE)

    def finish(self) -> None:
        if self.phase not in {Phase.REPORTED, Phase.SUSPEND_ELIGIBLE}:
            raise DeviceWorkflowError("invalid_workflow_transition")
        self.phase = Phase.FINISHED

    def block(self) -> None:
        if self.phase == Phase.FINISHED:
            raise DeviceWorkflowError("invalid_workflow_transition")
        self.phase = Phase.BLOCKED
