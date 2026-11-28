from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from max_core import pc_control
from max_core.assistant_session import AssistantSession, SessionOptions
from max_core.assistant_tools import assistant_registry
from max_core.gateway import InferenceDestination
from max_core.tools import ToolError


def test_parse_keys_accepts_shortcuts_and_rejects_unknown() -> None:
    assert pc_control.parse_keys("ctrl+l") == [0x11, ord("L")]
    assert pc_control.parse_keys("Win + D") == [0x5B, ord("D")]
    assert pc_control.parse_keys("volumeup") == [0xAF]
    for bad in ("", "ctrl+", "ctrl+banana", "a+b+c+d+e+f"):
        with pytest.raises(ToolError):
            pc_control.parse_keys(bad)


def test_full_control_needs_exact_opt_in() -> None:
    assert pc_control.auto_approved("pc.click", {"MAX_PC_CONTROL": "full"})
    assert pc_control.auto_approved("apps.open", {"MAX_PC_CONTROL": " FULL "})
    assert not pc_control.auto_approved("pc.click", {})
    assert not pc_control.auto_approved("pc.click", {"MAX_PC_CONTROL": "yes"})
    assert not pc_control.auto_approved("shell.run", {"MAX_PC_CONTROL": "full"})
    assert not pc_control.auto_approved("memory.forget", {"MAX_PC_CONTROL": "full"})


def test_session_options_read_pc_control() -> None:
    assert SessionOptions.from_env({"MAX_PC_CONTROL": "full"}).pc_control
    assert not SessionOptions.from_env({}).pc_control


def _session(
    *,
    pc_control: bool,
    cloud: bool,
    host_tools: bool = True,
    tv_env: dict[str, str] | None = None,
    destination: InferenceDestination = InferenceDestination.VERIFIED_LOCAL,
) -> AssistantSession:
    session = AssistantSession.__new__(AssistantSession)
    session.options = SessionOptions(
        data_dir=Path("."),
        workspace=None,
        cloud_personal_tools=False,
        enable_shell=False,
        enable_web=False,
        host_tools=host_tools,
        pc_control=pc_control,
        tv_env=tv_env,
    )
    session.settings = SimpleNamespace(  # type: ignore[assignment]
        spec=SimpleNamespace(
            inference_destination=InferenceDestination.CLOUD if cloud else destination
        )
    )
    session.is_cloud = cloud
    return session


def test_local_full_control_skips_prompt_only_for_pc_tools() -> None:
    asked: list[str] = []

    def owner(name: str, _args: dict) -> bool:
        asked.append(name)
        return False

    decide = _session(pc_control=True, cloud=False)._with_pc_control(owner)
    assert decide is not None
    assert decide("pc.type_text", {"text": "hi"}) is True
    assert decide("apps.open", {"target": "chrome"}) is True
    assert asked == []
    assert decide("shell.run", {"command": "dir"}) is False
    assert asked == ["shell.run"]


@pytest.mark.parametrize(
    ("pc_control", "cloud", "host_tools"),
    [(False, False, True), (True, True, True), (True, False, False)],
)
def test_prompt_kept_when_off_cloud_or_hosted(
    pc_control: bool, cloud: bool, host_tools: bool
) -> None:
    def owner(_name: str, _args: dict) -> bool:
        return False

    session = _session(pc_control=pc_control, cloud=cloud, host_tools=host_tools)
    assert session._with_pc_control(owner) is owner


@pytest.mark.parametrize(
    ("cloud", "host_tools", "destination", "allowed"),
    [
        (False, True, InferenceDestination.VERIFIED_LOCAL, True),
        (False, True, InferenceDestination.UNKNOWN, False),
        (True, True, InferenceDestination.CLOUD, False),
        (False, False, InferenceDestination.VERIFIED_LOCAL, False),
    ],
)
def test_tv_auto_approval_respects_session_boundary(
    cloud: bool, host_tools: bool, destination: InferenceDestination, allowed: bool
) -> None:
    session = _session(
        pc_control=False,
        cloud=cloud,
        host_tools=host_tools,
        destination=destination,
        tv_env={"MAX_TV_CONTROL": "auto"},
    )
    decide = session._with_pc_control(lambda _name, _args: False)
    assert decide is not None
    assert decide("tv.press", {"key": "home"}) is allowed
    assert decide("tv.unreviewed", {}) is False
    assert decide("shell.run", {"command": "dir"}) is False


def test_pc_tools_only_offered_on_local_windows(tmp_path: Path) -> None:
    names = {t.name for t in assistant_registry(data_dir=tmp_path, enable_web=False)}
    assert ("pc.click" in names) == (sys.platform == "win32")
    cloud = {t.name for t in assistant_registry(data_dir=tmp_path, enable_web=False, cloud=True)}
    assert "pc.click" not in cloud


def test_handlers_refuse_off_windows() -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX-only check")
    with pytest.raises(ToolError, match="windows_desktop_required"):
        pc_control.type_text({"text": "x"})
