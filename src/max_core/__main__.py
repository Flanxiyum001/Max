"""Start the local development server with explicit fail-closed configuration."""

from __future__ import annotations

import sys

import uvicorn

from max_core.api import create_app
from max_core.config import ConfigurationError, Settings


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        print(f"MAX Core configuration error: {exc}", file=sys.stderr)
        return 2

    uvicorn.run(
        create_app(settings),
        host=settings.bind_host,
        port=settings.bind_port,
        log_level=settings.log_level.lower(),
        access_log=False,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
