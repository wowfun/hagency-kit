from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

PROTOCOL_RESPONSES = "openai_responses"
PROTOCOL_CHAT = "openai_chat_completions"
PROTOCOLS = frozenset({PROTOCOL_RESPONSES, PROTOCOL_CHAT})


class ProviderAdapterError(ValueError):
    pass


def _default_operation_paths() -> Mapping[str, str]:
    return MappingProxyType(
        {
            PROTOCOL_RESPONSES: "responses",
            PROTOCOL_CHAT: "chat/completions",
        }
    )


@dataclass(frozen=True)
class ProviderAdapter:
    """Declarative provider seam used by config loading and request routing."""

    name: str
    default_protocol: str | None = None
    default_base_url: str | None = None
    api_key_header: str | None = "Authorization"
    api_key_prefix: str = "Bearer "
    operation_paths: Mapping[str, str] = field(default_factory=_default_operation_paths)

    def validate(self) -> None:
        if not self.name:
            raise ProviderAdapterError("adapter name must not be empty")
        if self.default_protocol is not None and self.default_protocol not in PROTOCOLS:
            raise ProviderAdapterError(
                f"adapter {self.name} has an unsupported default protocol"
            )
        if not self.operation_paths:
            raise ProviderAdapterError(
                f"adapter {self.name} must define at least one operation path"
            )
        for protocol, path in self.operation_paths.items():
            if protocol not in PROTOCOLS:
                raise ProviderAdapterError(
                    f"adapter {self.name} declares an unsupported protocol: {protocol}"
                )
            if (
                not isinstance(path, str)
                or not path
                or path.startswith("/")
                or any(part in {"", ".", ".."} for part in path.split("/"))
            ):
                raise ProviderAdapterError(
                    f"adapter {self.name} has an invalid operation path for {protocol}"
                )
        if (
            self.default_protocol is not None
            and self.default_protocol not in self.operation_paths
        ):
            raise ProviderAdapterError(
                f"adapter {self.name} does not implement its default protocol"
            )
        if self.api_key_header is not None and (
            not self.api_key_header
            or any(char in self.api_key_header for char in "\r\n:")
        ):
            raise ProviderAdapterError(
                f"adapter {self.name} has an invalid API key header"
            )
        if any(char in self.api_key_prefix for char in "\r\n"):
            raise ProviderAdapterError(
                f"adapter {self.name} has an invalid API key prefix"
            )

    def resolve_protocol(self, configured: str | None) -> str:
        protocol = self.default_protocol if configured is None else configured
        if protocol is None:
            raise ProviderAdapterError(
                f"adapter {self.name} requires an explicit protocol"
            )
        if protocol not in PROTOCOLS:
            raise ProviderAdapterError(f"unsupported protocol: {protocol}")
        if protocol not in self.operation_paths:
            raise ProviderAdapterError(
                f"adapter {self.name} does not support protocol {protocol}"
            )
        return protocol

    def resolve_base_url(self, configured: str | None) -> str:
        base_url = self.default_base_url if configured is None else configured
        if base_url is None:
            raise ProviderAdapterError(
                f"adapter {self.name} requires an explicit base_url"
            )
        return base_url

    def api_key_headers(self, api_key: str) -> tuple[tuple[str, str], ...]:
        if self.api_key_header is None:
            raise ProviderAdapterError(
                f"adapter {self.name} does not support the api_key shortcut"
            )
        return ((self.api_key_header, f"{self.api_key_prefix}{api_key}"),)

    def operation_path(self, protocol: str, tail: str = "") -> str:
        try:
            prefix = self.operation_paths[protocol]
        except KeyError as exc:
            raise ProviderAdapterError(
                f"adapter {self.name} does not support protocol {protocol}"
            ) from exc
        return prefix if not tail else f"{prefix}/{tail}"
