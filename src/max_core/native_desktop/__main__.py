"""Launch MAX's native window with python -m max_core.native_desktop."""

from __future__ import annotations

import os
import sys


def main() -> int:
    args = sys.argv[1:]
    allowed = {"--smoke-test", "--dashboard", "--magnitude", "--check-magnitude"}
    if (
        set(args) - allowed
        or len(set(args)) != len(args)
        or ("--check-magnitude" in args and len(args) != 1)
        or ("--magnitude" in args and "--dashboard" in args)
    ):
        print(
            "Usage: python -m max_core.native_desktop "
            "[--dashboard | --magnitude] [--smoke-test] or --check-magnitude",
            file=sys.stderr,
        )
        return 2
    if "--magnitude" in args or "--check-magnitude" in args:
        from max_core.assistant_session import merged_environment
        from max_core.gateway_trial import TrialConfigurationError
        from max_core.magnitude import check, describe_error, magnitude_settings

        env = merged_environment()
        try:
            model = check(configured=env.get("MAX_MAGNITUDE_MODEL", ""))
            magnitude_settings({**env, "MAX_MAGNITUDE_MODEL": model})
        except TrialConfigurationError as exc:
            print(describe_error(exc.code), file=sys.stderr)
            return 2
        print(f"[MAX] Magnitude model: {model}")
        if "--check-magnitude" in args:
            return 0
        os.environ["MAX_LOCAL_PROVIDER"] = "magnitude"
        os.environ["MAX_MAGNITUDE_MODEL"] = model
    try:
        from max_core.native_desktop.orb_ui import launch_orb
        from max_core.native_desktop.qt_ui import launch
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("PySide6"):
            print(
                "MAX Desktop requires optional Qt dependencies. "
                "Run: python -m pip install -e '.[desktop]'",
                file=sys.stderr,
            )
            return 2
        raise
    smoke = "--smoke-test" in args
    if "--dashboard" in args:
        return launch(smoke_test=smoke)
    return launch_orb(smoke_test=smoke)


if __name__ == "__main__":
    raise SystemExit(main())
