"""Start the local development server with explicit fail-closed configuration."""

from __future__ import annotations

import sys

import uvicorn

from max_core.api import create_app
from max_core.config import ConfigurationError, Settings
from max_core.triggers import TriggerLoop


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        print(f"MAX Core configuration error: {exc}", file=sys.stderr)
        return 2

    app = create_app(settings)
    trigger_loop: TriggerLoop | None = None
    if app.state.trigger_service is not None:
        trigger_loop = TriggerLoop(app.state.trigger_service)
        trigger_loop.start()
    try:
        uvicorn.run(
            app,
            host=settings.bind_host,
            port=settings.bind_port,
            log_level=settings.log_level.lower(),
            access_log=False,
            server_header=False,
        )
    finally:
        if trigger_loop is not None:
            trigger_loop.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
