"""Launch MAX's native window with python -m max_core.native_desktop."""

from __future__ import annotations

import sys


def main() -> int:
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
    args = sys.argv[1:]
    if any(arg not in {"--smoke-test", "--dashboard"} for arg in args) or len(set(args)) != len(
        args
    ):
        print(
            "Usage: python -m max_core.native_desktop [--dashboard] [--smoke-test]",
            file=sys.stderr,
        )
        return 2
    smoke = "--smoke-test" in args
    if "--dashboard" in args:
        return launch(smoke_test=smoke)
    return launch_orb(smoke_test=smoke)


if __name__ == "__main__":
    raise SystemExit(main())
