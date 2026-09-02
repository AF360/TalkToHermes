from __future__ import annotations

import argparse
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import uvicorn

from .app import create_app
from .config import EXPECTED_PORT, ConfigError, is_private_non_loopback_ipv4, load_config


class ListenerError(RuntimeError):
    """Raised when the dedicated listener cannot be used safely."""


def validate_listener_available(
    host: str,
    port: int,
    *,
    socket_factory: Callable[..., Any] = socket.socket,
) -> None:
    if not is_private_non_loopback_ipv4(host) or port != EXPECTED_PORT:
        raise ListenerError(
            "dedicated listener must use a private non-loopback IPv4 address on port 9090"
        )
    listener = socket_factory(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind((host, port))
    except OSError as exc:
        raise ListenerError("dedicated listener is unavailable") from exc
    finally:
        listener.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="TalkToHermes OmniVoice service")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        settings = load_config(args.config)
        validate_listener_available(settings.listen_host, settings.listen_port)
    except (ConfigError, ListenerError):
        parser.exit(2, "omnivoice: startup validation failed\n")
    uvicorn.run(
        create_app(settings),
        host=settings.listen_host,
        port=settings.listen_port,
        access_log=False,
        server_header=False,
        workers=1,
        limit_concurrency=8,
        timeout_keep_alive=5,
        backlog=16,
    )


if __name__ == "__main__":
    main()
