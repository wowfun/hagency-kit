from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Annotated

import typer

from hagency_cli.commands.completion import complete_file
from hagency_cli.commands.options import WorkspaceRootOption
from hagency_cli.commands.shared import (
    command_errors,
    die,
    make_app,
    require_at_most_one,
)
from hagency_cli.model_proxy import ModelProxyConfigError
from hagency_cli.model_proxy.daemon import (
    ModelProxyServiceError,
    restart_model_proxy,
    start_model_proxy,
    stop_model_proxy,
)
from hagency_cli.paths import expand_path
from hagency_cli.workspace.context import workspace_root_arg

service_app = make_app(
    help_text="Manage local background services.",
    add_completion=False,
)
model_proxy_app = make_app(
    help_text="Run the local model proxy.",
    add_completion=False,
)
service_app.add_typer(model_proxy_app, name="model-proxy")


def model_proxy_config_path(*, root: str | None, config: str | None) -> Path:
    require_at_most_one({"--root": root, "--config": config})
    if config is not None:
        return expand_path(config, Path.cwd())
    return workspace_root_arg(root) / "hagency-model-proxy.toml"


def validate_model_proxy_host(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise typer.BadParameter(
            "host must be a loopback IP address", param_hint="--host"
        ) from exc
    if not address.is_loopback:
        raise typer.BadParameter(
            "host must be a loopback IP address", param_hint="--host"
        )


def model_proxy_url(host: str, port: int) -> str:
    display_host = f"[{host}]" if ":" in host else host
    return f"http://{display_host}:{port}"


def start_model_proxy_command(
    *,
    root: str | None,
    config: str | None,
    host: str,
    port: int,
    debug: bool = False,
) -> None:
    config_path = model_proxy_config_path(root=root, config=config)
    validate_model_proxy_host(host)
    try:
        state, paths = start_model_proxy(config_path, host=host, port=port, debug=debug)
    except (ModelProxyConfigError, ModelProxyServiceError) as exc:
        die(str(exc))
    print(
        f"started model proxy: pid {state.pid}, "
        f"{model_proxy_url(state.host, state.port)}"
    )
    print(f"log: {paths.log}")
    print(f"debug: {'on' if state.debug else 'off'}")
    if state.debug:
        print(f"debug logs: {paths.debug_directory}")


def stop_model_proxy_command(*, root: str | None, config: str | None) -> None:
    config_path = model_proxy_config_path(root=root, config=config)
    try:
        stopped, _paths = stop_model_proxy(config_path)
    except ModelProxyServiceError as exc:
        die(str(exc))
    print("stopped model proxy" if stopped else "model proxy is not running")


def restart_model_proxy_command(
    *,
    root: str | None,
    config: str | None,
    host: str,
    port: int,
    debug: bool = False,
) -> None:
    config_path = model_proxy_config_path(root=root, config=config)
    validate_model_proxy_host(host)
    try:
        state, paths = restart_model_proxy(
            config_path, host=host, port=port, debug=debug
        )
    except (ModelProxyConfigError, ModelProxyServiceError) as exc:
        die(str(exc))
    print(
        f"restarted model proxy: pid {state.pid}, "
        f"{model_proxy_url(state.host, state.port)}"
    )
    print(f"log: {paths.log}")
    print(f"debug: {'on' if state.debug else 'off'}")
    if state.debug:
        print(f"debug logs: {paths.debug_directory}")


@model_proxy_app.command("start", short_help="Start the model proxy in the background.")
@command_errors
def serve_start_cli(
    root: WorkspaceRootOption = None,
    config: Annotated[
        str | None,
        typer.Option(
            "--config",
            help="Proxy config file (mutually exclusive with --root)",
            autocompletion=complete_file,
        ),
    ] = None,
    host: Annotated[
        str, typer.Option("--host", help="Loopback IP address to listen on")
    ] = "127.0.0.1",
    port: Annotated[
        int, typer.Option("--port", min=1, max=65535, help="TCP port to listen on")
    ] = 8765,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help="Log full request and response bodies per provider",
        ),
    ] = False,
) -> None:
    """Start a background proxy and report its PID, address and log after binding.
    Use the same config path to stop or restart this worker.
    """
    start_model_proxy_command(
        root=root, config=config, host=host, port=port, debug=debug
    )


@model_proxy_app.command("stop", short_help="Stop the background model proxy.")
@command_errors
def serve_stop_cli(
    root: WorkspaceRootOption = None,
    config: Annotated[
        str | None,
        typer.Option(
            "--config",
            help="Proxy config file (mutually exclusive with --root)",
            autocompletion=complete_file,
        ),
    ] = None,
) -> None:
    """Stop the worker for the config path and state directory used at startup.

    An already stopped worker counts as success.
    """
    stop_model_proxy_command(root=root, config=config)


@model_proxy_app.command("restart", short_help="Restart the background model proxy.")
@command_errors
def serve_restart_cli(
    root: WorkspaceRootOption = None,
    config: Annotated[
        str | None,
        typer.Option(
            "--config",
            help="Proxy config file (mutually exclusive with --root)",
            autocompletion=complete_file,
        ),
    ] = None,
    host: Annotated[
        str, typer.Option("--host", help="Loopback IP address to listen on")
    ] = "127.0.0.1",
    port: Annotated[
        int, typer.Option("--port", min=1, max=65535, help="TCP port to listen on")
    ] = 8765,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help="Log full request and response bodies per provider",
        ),
    ] = False,
) -> None:
    """Restart the worker for the selected config to reload settings and hooks.
    Repeat custom listener options. If startup fails, the old worker remains stopped.
    """
    restart_model_proxy_command(
        root=root, config=config, host=host, port=port, debug=debug
    )
