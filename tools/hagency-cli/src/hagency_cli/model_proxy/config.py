from __future__ import annotations

import ipaddress
import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Never
from urllib.parse import urlsplit

from dotenv import dotenv_values

from .providers import ProviderAdapter, ProviderAdapterError, load_provider_adapter

PROVIDER_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
RESERVED_PROVIDER_NAMES = frozenset({"v1"})


class ModelProxyConfigError(ValueError):
    pass


def validate_loopback_host(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ModelProxyConfigError("host must be a loopback IP address") from exc
    if not address.is_loopback:
        raise ModelProxyConfigError("host must be a loopback IP address")


@dataclass(frozen=True)
class ValueSource:
    literal: str | None = None
    env: str | None = None
    prefix: str = ""

    def resolve(self, environ: Mapping[str, str]) -> str:
        if self.literal is not None:
            return self.literal
        assert self.env is not None
        try:
            return f"{self.prefix}{environ[self.env]}"
        except KeyError as exc:
            raise ModelProxyConfigError(
                f"missing environment variable: {self.env}"
            ) from exc


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    adapter: ProviderAdapter
    protocol: str
    base_url: str
    headers: tuple[tuple[str, str], ...] = ()
    query: tuple[tuple[str, str], ...] = ()
    forward_credential_headers: frozenset[str] = frozenset()
    hook: str | None = None
    hook_options: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    hook_timeout_seconds: float = 10.0
    connect_timeout_seconds: float = 10.0
    idle_timeout_seconds: float = 300.0


@dataclass(frozen=True)
class ProxyConfig:
    path: Path
    default_provider: str
    providers: Mapping[str, ProviderConfig]
    env: Mapping[str, str]


def _fail(path: str, message: str) -> Never:
    raise ModelProxyConfigError(f"{path}: {message}")


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be a table")
    return value


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        _fail(path, f"unknown field: {unknown[0]}")


def _positive_number(value: Any, path: str, default: float) -> float:
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        _fail(path, "must be a finite positive number")
    return float(value)


def _header_name(value: Any, path: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(char in value for char in "\r\n:")
    ):
        _fail(path, "must be a valid HTTP header name")
    return value


def _safe_value(value: str, path: str) -> str:
    if any(char in value for char in "\r\n"):
        _fail(path, "must not contain CR or LF")
    return value


def _value_source(raw: Any, path: str) -> ValueSource:
    if isinstance(raw, str):
        return ValueSource(literal=_safe_value(raw, path))
    mapping = _require_mapping(raw, path)
    _reject_unknown(mapping, {"env", "prefix"}, path)
    env = mapping.get("env")
    prefix = mapping.get("prefix", "")
    if not isinstance(env, str) or not env:
        _fail(f"{path}.env", "must be a non-empty string")
    if not isinstance(prefix, str):
        _fail(f"{path}.prefix", "must be a string")
    return ValueSource(env=env, prefix=_safe_value(prefix, f"{path}.prefix"))


def _resolved_pairs(
    raw: Any, path: str, environ: Mapping[str, str], *, headers: bool
) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    mapping = _require_mapping(raw, path)
    pairs: list[tuple[str, str]] = []
    for key, value in mapping.items():
        if not isinstance(key, str) or not key:
            _fail(path, "keys must be non-empty strings")
        name = _header_name(key, f"{path}.{key}") if headers else key
        if headers and name.lower() in {
            "connection",
            "content-length",
            "host",
            "keep-alive",
            "proxy-authenticate",
            "te",
            "trailer",
            "transfer-encoding",
            "upgrade",
        }:
            _fail(f"{path}.{key}", "cannot configure a transport-managed header")
        value_path = f"{path}.{key}"
        resolved = _value_source(value, value_path).resolve(environ)
        pairs.append((name, _safe_value(resolved, value_path)))
    return tuple(pairs)


def _base_url(raw: Any, path: str) -> str:
    if not isinstance(raw, str) or not raw:
        _fail(path, "must be a non-empty string")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        _fail(path, "must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        _fail(path, "must not contain userinfo")
    if parsed.query or parsed.fragment:
        _fail(path, "must not contain query or fragment")
    segments = [segment for segment in parsed.path.rstrip("/").split("/") if segment]
    if (segments and segments[-1] in {"models", "responses"}) or segments[-2:] == [
        "chat",
        "completions",
    ]:
        _fail(path, "must be an API root, not an operation URL")
    return raw.rstrip("/")


def _forward_headers(raw: Any, path: str) -> frozenset[str]:
    if raw is None:
        return frozenset()
    if not isinstance(raw, list) or any(
        not isinstance(item, str) or not item for item in raw
    ):
        _fail(path, "must be an array of header names")
    return frozenset(_header_name(item, path).lower() for item in raw)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _workspace_environment(
    path: Path, environ: Mapping[str, str] | None
) -> Mapping[str, str]:
    dotenv_path = path.resolve().parent / ".env"
    try:
        workspace_values = dotenv_values(dotenv_path)
    except (OSError, UnicodeError) as exc:
        raise ModelProxyConfigError(
            f"could not read environment file: {dotenv_path}"
        ) from exc
    merged = {
        name: value for name, value in workspace_values.items() if value is not None
    }
    merged.update(os.environ if environ is None else environ)
    return MappingProxyType(merged)


def load_proxy_config(
    path: Path, *, environ: Mapping[str, str] | None = None
) -> ProxyConfig:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ModelProxyConfigError(f"missing config: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ModelProxyConfigError(f"invalid TOML in {path}: {exc}") from exc

    resolved_environ = _workspace_environment(path, environ)

    _reject_unknown(raw, {"version", "default_provider", "providers"}, "config")
    if raw.get("version") != 1:
        _fail("version", "must be 1")
    default_provider = raw.get("default_provider")
    if not isinstance(default_provider, str) or not default_provider:
        _fail("default_provider", "must be a non-empty string")

    raw_providers = _require_mapping(raw.get("providers"), "providers")
    if not raw_providers:
        _fail("providers", "must contain at least one provider")
    providers: dict[str, ProviderConfig] = {}
    allowed = {
        "adapter",
        "protocol",
        "base_url",
        "api_key",
        "headers",
        "query",
        "forward_credential_headers",
        "hook",
        "hook_options",
        "hook_timeout_seconds",
        "connect_timeout_seconds",
        "idle_timeout_seconds",
    }
    for name, provider_raw in raw_providers.items():
        provider_path = f"providers.{name}"
        if (
            not PROVIDER_NAME_RE.fullmatch(name)
            or name.lower() in RESERVED_PROVIDER_NAMES
        ):
            _fail(provider_path, "invalid or reserved provider name")
        mapping = _require_mapping(provider_raw, provider_path)
        _reject_unknown(mapping, allowed, provider_path)
        adapter_name = mapping.get("adapter")
        if not isinstance(adapter_name, str) or not adapter_name:
            _fail(f"{provider_path}.adapter", "must be a non-empty string")
        try:
            adapter = load_provider_adapter(adapter_name)
        except ProviderAdapterError as exc:
            _fail(f"{provider_path}.adapter", str(exc))

        configured_protocol = mapping.get("protocol")
        if configured_protocol is not None and not isinstance(configured_protocol, str):
            _fail(f"{provider_path}.protocol", "must be a string")
        try:
            protocol = adapter.resolve_protocol(configured_protocol)
        except ProviderAdapterError as exc:
            _fail(
                f"{provider_path}.protocol",
                str(exc),
            )
        configured_base_url = mapping.get("base_url")
        if configured_base_url is not None and not isinstance(configured_base_url, str):
            _fail(f"{provider_path}.base_url", "must be a string")
        try:
            base_url = adapter.resolve_base_url(configured_base_url)
        except ProviderAdapterError as exc:
            _fail(f"{provider_path}.base_url", str(exc))

        headers = _resolved_pairs(
            mapping.get("headers"),
            f"{provider_path}.headers",
            resolved_environ,
            headers=True,
        )
        api_key_raw = mapping.get("api_key")
        if api_key_raw is not None:
            api_key_path = f"{provider_path}.api_key"
            api_key = _safe_value(
                _value_source(api_key_raw, api_key_path).resolve(resolved_environ),
                api_key_path,
            )
            try:
                api_key_headers = adapter.api_key_headers(api_key)
            except ProviderAdapterError as exc:
                _fail(f"{provider_path}.api_key", str(exc))
            conflicts = {name.lower() for name, _value in headers} & {
                name.lower() for name, _value in api_key_headers
            }
            if conflicts:
                _fail(
                    f"{provider_path}.headers",
                    f"must not redefine api_key header: {sorted(conflicts)[0]}",
                )
            headers = api_key_headers + headers
        hook = mapping.get("hook")
        if hook is not None and (not isinstance(hook, str) or not hook):
            _fail(f"{provider_path}.hook", "must be a non-empty string")
        hook_options = mapping.get("hook_options", {})
        _require_mapping(hook_options, f"{provider_path}.hook_options")
        providers[name] = ProviderConfig(
            name=name,
            adapter=adapter,
            protocol=protocol,
            base_url=_base_url(base_url, f"{provider_path}.base_url"),
            headers=headers,
            query=_resolved_pairs(
                mapping.get("query"),
                f"{provider_path}.query",
                resolved_environ,
                headers=False,
            ),
            forward_credential_headers=_forward_headers(
                mapping.get("forward_credential_headers"),
                f"{provider_path}.forward_credential_headers",
            ),
            hook=hook,
            hook_options=_freeze(dict(hook_options)),
            hook_timeout_seconds=_positive_number(
                mapping.get("hook_timeout_seconds"),
                f"{provider_path}.hook_timeout_seconds",
                10.0,
            ),
            connect_timeout_seconds=_positive_number(
                mapping.get("connect_timeout_seconds"),
                f"{provider_path}.connect_timeout_seconds",
                10.0,
            ),
            idle_timeout_seconds=_positive_number(
                mapping.get("idle_timeout_seconds"),
                f"{provider_path}.idle_timeout_seconds",
                300.0,
            ),
        )

    if default_provider not in providers:
        _fail("default_provider", "must name a configured provider")
    return ProxyConfig(
        path=path.resolve(),
        default_provider=default_provider,
        providers=MappingProxyType(providers),
        env=resolved_environ,
    )
