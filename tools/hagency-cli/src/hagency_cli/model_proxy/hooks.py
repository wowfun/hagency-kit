from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .config import ModelProxyConfigError, ProviderConfig
from .sse import SseEvent


@dataclass(frozen=True)
class HeaderView:
    items: tuple[tuple[str, str], ...] = ()

    def get(self, name: str, default: str | None = None) -> str | None:
        lowered = name.lower()
        for key, value in reversed(self.items):
            if key.lower() == lowered:
                return value
        return default

    def getall(self, name: str) -> tuple[str, ...]:
        lowered = name.lower()
        return tuple(value for key, value in self.items if key.lower() == lowered)


@dataclass(frozen=True)
class HeaderPatch:
    set: tuple[tuple[str, str], ...] = ()
    remove: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueryPatch:
    set: tuple[tuple[str, str], ...] = ()
    remove: tuple[str, ...] = ()


@dataclass(frozen=True)
class HookInit:
    provider: str
    upstream_protocol: str
    options: Mapping[str, Any]
    logger: SafeHookLogger


@dataclass(frozen=True)
class HookContext:
    request_id: str
    provider: str
    downstream_protocol: str
    upstream_protocol: str
    stream: bool
    state: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderRequest:
    method: str
    url: str
    headers: HeaderView
    body: bytes
    protocol: str

    def json(self) -> dict[str, Any]:
        import json

        value = json.loads(self.body)
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value


@dataclass(frozen=True)
class RequestPatch:
    json_body: dict[str, Any] | None = None
    headers: HeaderPatch = HeaderPatch()
    query: QueryPatch = QueryPatch()


@dataclass(frozen=True)
class FinalRequest:
    method: str
    url: str
    headers: HeaderView
    body: bytes
    protocol: str


@dataclass(frozen=True)
class AuthPatch:
    headers: HeaderPatch = HeaderPatch()
    query: QueryPatch = QueryPatch()


SseMapper = Callable[[HookContext, SseEvent], Awaitable[Sequence[SseEvent] | None]]


@dataclass(frozen=True)
class ProviderResponse:
    status: int
    headers: HeaderView
    protocol: str
    is_sse: bool
    body: bytes | None

    def json(self) -> dict[str, Any]:
        import json

        if self.body is None:
            raise ValueError("streaming response has no buffered body")
        value = json.loads(self.body)
        if not isinstance(value, dict):
            raise ValueError("response body must be a JSON object")
        return value


@dataclass(frozen=True)
class ResponsePatch:
    status: int | None = None
    headers: HeaderPatch = HeaderPatch()
    json_body: dict[str, Any] | None = None
    sse_mapper: SseMapper | None = None


class HookReject(Exception):
    def __init__(self, *, status: int, code: str, public_message: str) -> None:
        if status not in {400, 401, 403, 409, 422, 429}:
            raise ValueError("HookReject status must be an allowed 4xx status")
        super().__init__(public_message)
        self.status = status
        self.code = code
        self.public_message = public_message


class SafeHookLogger:
    _ALLOWED = frozenset({"event", "request_id", "provider", "stage", "code"})
    _SAFE_VALUE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")

    def __init__(self, logger: logging.Logger, provider: str) -> None:
        self._logger = logger
        self._provider = provider

    def info(self, event: str, **fields: str) -> None:
        safe = {
            key: value if self._SAFE_VALUE.fullmatch(value) else "<redacted>"
            for key, value in fields.items()
            if key in self._ALLOWED and isinstance(value, str)
        }
        safe_event = (
            event
            if isinstance(event, str) and self._SAFE_VALUE.fullmatch(event)
            else "hook_event"
        )
        self._logger.info(
            "hook event=%s provider=%s fields=%r",
            safe_event,
            self._provider,
            safe,
        )


@dataclass(frozen=True)
class HookRuntime:
    instance: object
    prepare_request: Callable[..., Awaitable[RequestPatch | None]] | None
    authenticate: Callable[..., Awaitable[AuthPatch | None]] | None
    process_response: Callable[..., Awaitable[ResponsePatch | None]] | None
    timeout_seconds: float

    async def call(
        self, method: Callable[..., Awaitable[Any]] | None, *args: object
    ) -> Any:
        if method is None:
            return None
        return await asyncio.wait_for(method(*args), timeout=self.timeout_seconds)


def _method(
    instance: object, name: str, provider: str
) -> Callable[..., Awaitable[Any]] | None:
    value = getattr(instance, name, None)
    if value is None:
        return None
    if not inspect.iscoroutinefunction(value):
        raise ModelProxyConfigError(
            f"provider {provider} hook method {name} must be async def"
        )
    return value


def load_hook(
    provider: ProviderConfig, config_path: Path, logger: logging.Logger
) -> HookRuntime | None:
    if provider.hook is None:
        return None
    relative = Path(provider.hook)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".py":
        raise ModelProxyConfigError(
            f"provider {provider.name} hook must be a relative .py file under hooks/"
        )
    hooks_root = (config_path.parent / "hooks").resolve()
    hook_path = (hooks_root / relative).resolve()
    if not hook_path.is_relative_to(hooks_root) or not hook_path.is_file():
        raise ModelProxyConfigError(
            f"provider {provider.name} hook not found under hooks/: {provider.hook}"
        )

    module_name = f"_hagency_model_proxy_hook_{provider.name}_{abs(hash(hook_path))}"
    spec = importlib.util.spec_from_file_location(module_name, hook_path)
    if spec is None or spec.loader is None:
        raise ModelProxyConfigError(f"could not load provider {provider.name} hook")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        hook_type = module.Hook
        instance = hook_type(
            HookInit(
                provider=provider.name,
                upstream_protocol=provider.protocol,
                options=MappingProxyType(dict(provider.hook_options)),
                logger=SafeHookLogger(logger, provider.name),
            )
        )
        runtime = HookRuntime(
            instance=instance,
            prepare_request=_method(instance, "prepare_request", provider.name),
            authenticate=_method(instance, "authenticate", provider.name),
            process_response=_method(instance, "process_response", provider.name),
            timeout_seconds=provider.hook_timeout_seconds,
        )
    except ModelProxyConfigError:
        sys.modules.pop(module_name, None)
        raise
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise ModelProxyConfigError(
            f"could not initialize provider {provider.name} hook: {type(exc).__name__}"
        ) from exc
    return runtime
