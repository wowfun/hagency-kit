from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
from pathlib import Path

from aiohttp import web

from .config import load_proxy_config, validate_loopback_host
from .daemon import (
    STARTUP_NONCE_ENV,
    ServicePaths,
    ServiceState,
    process_identity,
    remove_service_state,
    service_paths,
    write_service_state,
)
from .debug import LOGS_KEY, SafeAccessLogger
from .server import create_model_proxy_app


async def serve(
    app: web.Application,
    config_path: Path,
    paths: ServicePaths,
    host: str,
    port: int,
    startup_nonce: str,
) -> None:
    validate_loopback_host(host)
    runner = web.AppRunner(
        app,
        access_log=logging.getLogger("hagency.model_proxy.access"),
        access_log_class=SafeAccessLogger,
    )
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    pid = os.getpid()
    identity = process_identity(pid)
    if identity is None:
        raise RuntimeError("could not determine model proxy process identity")
    write_service_state(
        paths.state,
        ServiceState(
            pid=pid,
            config=config_path,
            host=host,
            port=port,
            started_at=time.time(),
            process_identity=identity,
            startup_nonce=startup_nonce,
            debug=LOGS_KEY in app,
        ),
    )
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(event, stopping.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stopping.wait()
    finally:
        remove_service_state(paths.state, pid=pid)
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Hagency model proxy worker")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    validate_loopback_host(args.host)
    config_path = args.config.resolve()
    startup_nonce = os.environ.get(STARTUP_NONCE_ENV)
    if not startup_nonce:
        raise RuntimeError("missing model proxy startup nonce")
    paths = service_paths(config_path)
    config = load_proxy_config(config_path)
    app = create_model_proxy_app(
        config,
        debug_directory=paths.debug_directory if args.debug or config.debug else None,
    )
    asyncio.run(serve(app, config_path, paths, args.host, args.port, startup_nonce))


if __name__ == "__main__":
    main()
