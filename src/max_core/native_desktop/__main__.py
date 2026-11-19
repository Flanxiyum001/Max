"""Launch MAX's native window with python -m max_core.native_desktop."""

from __future__ import annotations

import sys


def main() -> int:
    try:
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
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] != "--smoke-test"):
        print("Usage: python -m max_core.native_desktop [--smoke-test]", file=sys.stderr)
        return 2
    return launch(smoke_test="--smoke-test" in sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
