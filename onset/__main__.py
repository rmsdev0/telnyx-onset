"""Minimal entrypoint: serve the media-stream FastAPI app.

Deliberately not a product CLI (deploy, packaging, and a polished CLI are out of
scope for this session). Runs the server with uvicorn on the configured host and
port.
"""

from __future__ import annotations

import uvicorn

from onset.settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "onset.server:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
