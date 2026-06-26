"""Run the server: ``python -m daintree_assistant_server``.

Binds to ``HOST``/``PORT`` from settings (loaded from ``.env`` locally, or from
the environment in Cloud Run, which injects ``PORT=8080``). This is the single
entrypoint for both local development and the container.
"""

from __future__ import annotations

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "daintree_assistant_server.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        timeout_graceful_shutdown=25,
    )


if __name__ == "__main__":
    main()
