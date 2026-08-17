from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .config import load_proxy_config, validate_loopback_host

START_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 10.0
LOG_ROTATION_BYTES = 10 * 1024 * 1024
LOG_ROTATION_BACKUPS = 3
WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200
WINDOWS_DETACHED_PROCESS = 0x00000008
WINDOWS_CREATE_NO_WINDOW = 0x08000000
_LOCAL_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WINDOWS_STILL_ACTIVE = 259
WINDOWS_ERROR_ACCESS_DENIED = 5


class ModelProxyServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServicePaths:
    directory: Path
    state: Path
    lock: Path
    log: Path


@dataclass(frozen=True)
class ServiceState:
    pid: int
    config: Path
    host: str
    port: int
    started_at: float
    process_identity: str


def service_paths(
    config_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> ServicePaths:
    environ = os.environ if environ is None else environ
    platform = os.name if platform is None else platform
    configured_root = environ.get("HAGENCY_STATE_HOME")
    if configured_root:
        root = Path(configured_root).expanduser()
        if not root.is_absolute():
            raise ModelProxyServiceError("HAGENCY_STATE_HOME must be an absolute path")
    elif platform == "nt" and environ.get("LOCALAPPDATA"):
        root = Path(environ["LOCALAPPDATA"]) / "Hagency"
    elif environ.get("XDG_STATE_HOME"):
        root = Path(environ["XDG_STATE_HOME"]) / "hagency"
    else:
        root = Path.home() / ".local" / "state" / "hagency"
    identity = hashlib.sha256(str(config_path.resolve()).encode()).hexdigest()[:16]
    directory = root / "model-proxy" / identity
    return ServicePaths(
        directory=directory,
        state=directory / "state.json",
        lock=directory / "control.lock",
        log=directory / "service.log",
    )


def _windows_pid_alive(pid: int) -> bool:
    try:
        ctypes_windows = cast(Any, ctypes)
        kernel32 = ctypes_windows.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        open_process.restype = ctypes.c_void_p
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        get_exit_code.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        handle = open_process(WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            return ctypes_windows.get_last_error() == WINDOWS_ERROR_ACCESS_DENIED
        try:
            exit_code = ctypes.c_ulong()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == WINDOWS_STILL_ACTIVE
        finally:
            close_handle(handle)
    except (AttributeError, OSError, TypeError):
        return False


class _WindowsFileTime(ctypes.Structure):
    _fields_ = (("low", ctypes.c_ulong), ("high", ctypes.c_ulong))


def _windows_process_identity(pid: int) -> str | None:
    try:
        ctypes_windows = cast(Any, ctypes)
        kernel32 = ctypes_windows.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        open_process.restype = ctypes.c_void_p
        get_process_times = kernel32.GetProcessTimes
        get_process_times.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_WindowsFileTime),
            ctypes.POINTER(_WindowsFileTime),
            ctypes.POINTER(_WindowsFileTime),
            ctypes.POINTER(_WindowsFileTime),
        ]
        get_process_times.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        handle = open_process(WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            return None
        try:
            creation = _WindowsFileTime()
            exit_time = _WindowsFileTime()
            kernel_time = _WindowsFileTime()
            user_time = _WindowsFileTime()
            if not get_process_times(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None
            return f"windows:{creation.high << 32 | creation.low}"
        finally:
            close_handle(handle)
    except (AttributeError, OSError, TypeError):
        return None


def process_identity(pid: int, *, platform: str | None = None) -> str | None:
    platform = os.name if platform is None else platform
    if platform == "nt":
        return _windows_process_identity(pid)
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    closing = stat.rfind(")")
    fields = stat[closing + 2 :].split() if closing >= 0 else []
    if len(fields) <= 19:
        return None
    return f"linux:{fields[19]}"


def _pid_alive(pid: int, *, platform: str | None = None) -> bool:
    if pid <= 0:
        return False
    local_process = _LOCAL_PROCESSES.get(pid)
    if local_process is not None:
        if local_process.poll() is None:
            return True
        _LOCAL_PROCESSES.pop(pid, None)
        return False
    platform = os.name if platform is None else platform
    if platform == "nt":
        return _windows_pid_alive(pid)
    if platform != "nt":
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        else:
            if waited == pid:
                return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_state(path: Path) -> ServiceState | None:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelProxyServiceError(f"invalid model proxy state file: {path}") from exc
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("pid"), int)
        or isinstance(raw.get("pid"), bool)
        or raw["pid"] <= 0
        or not isinstance(raw.get("config"), str)
        or not isinstance(raw.get("host"), str)
        or not isinstance(raw.get("port"), int)
        or isinstance(raw.get("port"), bool)
        or not 1 <= raw["port"] <= 65535
        or not isinstance(raw.get("started_at"), int | float)
        or isinstance(raw.get("started_at"), bool)
        or not math.isfinite(raw["started_at"])
        or raw["started_at"] < 0
        or not isinstance(raw.get("process_identity"), str)
        or not raw["process_identity"]
    ):
        raise ModelProxyServiceError(f"invalid model proxy state file: {path}")
    return ServiceState(
        pid=raw["pid"],
        config=Path(raw["config"]),
        host=raw["host"],
        port=raw["port"],
        started_at=float(raw["started_at"]),
        process_identity=raw["process_identity"],
    )


def write_service_state(path: Path, state: ServiceState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{state.pid}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "pid": state.pid,
                "config": str(state.config.resolve()),
                "host": state.host,
                "port": state.port,
                "started_at": state.started_at,
                "process_identity": state.process_identity,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def remove_service_state(path: Path, *, pid: int) -> None:
    try:
        state = _read_state(path)
    except ModelProxyServiceError:
        return
    if state is not None and state.pid == pid:
        path.unlink(missing_ok=True)


def _lock_descriptor(descriptor: int, *, platform: str) -> None:
    if platform == "nt":
        msvcrt = cast(Any, __import__("msvcrt"))

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_descriptor(descriptor: int, *, platform: str) -> None:
    if platform == "nt":
        msvcrt = cast(Any, __import__("msvcrt"))

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextlib.contextmanager
def _control_lock(paths: ServicePaths) -> Iterator[None]:
    paths.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        paths.directory.chmod(0o700)
    except OSError:
        pass
    descriptor = os.open(paths.lock, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        try:
            _lock_descriptor(descriptor, platform=os.name)
        except OSError as exc:
            raise ModelProxyServiceError(
                "another model proxy lifecycle command is already running"
            ) from exc
        locked = True
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                _unlock_descriptor(descriptor, platform=os.name)
        os.close(descriptor)


def _worker_command(config_path: Path, host: str, port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "hagency_cli.model_proxy.worker",
        "--config",
        str(config_path.resolve()),
        "--host",
        host,
        "--port",
        str(port),
    ]


def _worker_environment() -> dict[str, str]:
    environ = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[2])
    current = environ.get("PYTHONPATH")
    environ["PYTHONPATH"] = (
        source_root if not current else os.pathsep.join((source_root, current))
    )
    return environ


def _spawn_worker(
    command: list[str],
    log_handle: Any,
    *,
    environ: Mapping[str, str],
    platform: str | None = None,
) -> subprocess.Popen[bytes]:
    platform = os.name if platform is None else platform
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
        "env": dict(environ),
    }
    if platform == "nt":
        options["creationflags"] = (
            WINDOWS_CREATE_NEW_PROCESS_GROUP
            | WINDOWS_DETACHED_PROCESS
            | WINDOWS_CREATE_NO_WINDOW
        )
    else:
        options["start_new_session"] = True
    return cast(subprocess.Popen[bytes], subprocess.Popen(command, **options))


def _validate_start_config(config_path: Path) -> None:
    # Hook modules execute only in the worker process, where their runtime is used.
    load_proxy_config(config_path)


def _rotate_service_log(
    path: Path,
    *,
    max_bytes: int = LOG_ROTATION_BYTES,
    backups: int = LOG_ROTATION_BACKUPS,
) -> None:
    try:
        if path.stat().st_size < max_bytes:
            return
        path.with_name(f"{path.name}.{backups}").unlink(missing_ok=True)
        for index in range(backups - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            if source.exists():
                source.replace(path.with_name(f"{path.name}.{index + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ModelProxyServiceError(
            f"could not rotate model proxy log: {path}"
        ) from exc


def _start_model_proxy_locked(
    config_path: Path,
    paths: ServicePaths,
    *,
    host: str,
    port: int,
    start_timeout_seconds: float,
) -> tuple[ServiceState, ServicePaths]:
    current = _read_state(paths.state)
    if (
        current is not None
        and _pid_alive(current.pid)
        and process_identity(current.pid) == current.process_identity
    ):
        raise ModelProxyServiceError(
            f"model proxy is already running with pid {current.pid}"
        )
    paths.state.unlink(missing_ok=True)
    _rotate_service_log(paths.log)
    log_descriptor = os.open(paths.log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with contextlib.suppress(OSError):
        paths.log.chmod(0o600)
    with os.fdopen(log_descriptor, "ab", buffering=0) as log_handle:
        process = _spawn_worker(
            _worker_command(config_path, host, port),
            log_handle,
            environ=_worker_environment(),
        )
        _LOCAL_PROCESSES[process.pid] = process
    deadline = time.monotonic() + start_timeout_seconds
    while time.monotonic() < deadline:
        state = _read_state(paths.state)
        if state is not None and state.pid == process.pid and _pid_alive(state.pid):
            return state, paths
        return_code = process.poll()
        if return_code is not None:
            _LOCAL_PROCESSES.pop(process.pid, None)
            raise ModelProxyServiceError(
                f"model proxy failed to start (exit {return_code}); see {paths.log}"
            )
        time.sleep(0.05)
    process.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=2)
    _LOCAL_PROCESSES.pop(process.pid, None)
    raise ModelProxyServiceError(f"model proxy did not become ready; see {paths.log}")


def start_model_proxy(
    config_path: Path,
    *,
    host: str,
    port: int,
    start_timeout_seconds: float = START_TIMEOUT_SECONDS,
) -> tuple[ServiceState, ServicePaths]:
    config_path = config_path.resolve()
    validate_loopback_host(host)
    _validate_start_config(config_path)
    paths = service_paths(config_path)
    with _control_lock(paths):
        return _start_model_proxy_locked(
            config_path,
            paths,
            host=host,
            port=port,
            start_timeout_seconds=start_timeout_seconds,
        )


def _stop_model_proxy_locked(
    config_path: Path,
    paths: ServicePaths,
    *,
    stop_timeout_seconds: float,
) -> tuple[bool, ServicePaths]:
    state = _read_state(paths.state)
    if state is None:
        return False, paths
    if state.config.resolve() != config_path:
        raise ModelProxyServiceError("model proxy state belongs to another config")
    if (
        not _pid_alive(state.pid)
        or process_identity(state.pid) != state.process_identity
    ):
        paths.state.unlink(missing_ok=True)
        return False, paths
    try:
        os.kill(state.pid, signal.SIGTERM)
    except ProcessLookupError:
        paths.state.unlink(missing_ok=True)
        return False, paths
    deadline = time.monotonic() + stop_timeout_seconds
    while time.monotonic() < deadline and _pid_alive(state.pid):
        time.sleep(0.05)
    if _pid_alive(state.pid):
        if os.name == "nt":
            os.kill(state.pid, signal.SIGTERM)
        else:
            os.kill(state.pid, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _pid_alive(state.pid):
            time.sleep(0.05)
    paths.state.unlink(missing_ok=True)
    return True, paths


def stop_model_proxy(
    config_path: Path, *, stop_timeout_seconds: float = STOP_TIMEOUT_SECONDS
) -> tuple[bool, ServicePaths]:
    config_path = config_path.resolve()
    paths = service_paths(config_path)
    with _control_lock(paths):
        return _stop_model_proxy_locked(
            config_path, paths, stop_timeout_seconds=stop_timeout_seconds
        )


def restart_model_proxy(
    config_path: Path, *, host: str, port: int
) -> tuple[ServiceState, ServicePaths]:
    config_path = config_path.resolve()
    validate_loopback_host(host)
    _validate_start_config(config_path)
    paths = service_paths(config_path)
    with _control_lock(paths):
        _stop_model_proxy_locked(
            config_path, paths, stop_timeout_seconds=STOP_TIMEOUT_SECONDS
        )
        return _start_model_proxy_locked(
            config_path,
            paths,
            host=host,
            port=port,
            start_timeout_seconds=START_TIMEOUT_SECONDS,
        )


__all__ = [
    "ModelProxyServiceError",
    "ServicePaths",
    "ServiceState",
    "remove_service_state",
    "process_identity",
    "restart_model_proxy",
    "service_paths",
    "start_model_proxy",
    "stop_model_proxy",
    "write_service_state",
]
