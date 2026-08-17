from __future__ import annotations

import importlib
import re

from .base import (
    PROTOCOL_CHAT,
    PROTOCOL_RESPONSES,
    PROTOCOLS,
    ProviderAdapter,
    ProviderAdapterError,
)

ADAPTER_NAME_RE = re.compile(r"[a-z][a-z0-9_]*\Z")


def load_provider_adapter(name: str) -> ProviderAdapter:
    if not ADAPTER_NAME_RE.fullmatch(name):
        raise ProviderAdapterError(
            "adapter must use lowercase letters, numbers, and underscores"
        )
    module_name = f"{__name__}.{name}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            raise ProviderAdapterError(
                f"unknown adapter {name}; add providers/{name}.py exporting ADAPTER"
            ) from exc
        raise ProviderAdapterError(
            f"could not import adapter {name}: missing dependency {exc.name}"
        ) from exc
    except Exception as exc:
        raise ProviderAdapterError(
            f"could not import adapter {name}: {type(exc).__name__}"
        ) from exc

    adapter = getattr(module, "ADAPTER", None)
    if not isinstance(adapter, ProviderAdapter):
        raise ProviderAdapterError(
            f"adapter module providers/{name}.py must export ProviderAdapter as ADAPTER"
        )
    if adapter.name != name:
        raise ProviderAdapterError(
            f"adapter module {name} exports mismatched name {adapter.name}"
        )
    adapter.validate()
    return adapter


__all__ = [
    "PROTOCOL_CHAT",
    "PROTOCOL_RESPONSES",
    "PROTOCOLS",
    "ProviderAdapter",
    "ProviderAdapterError",
    "load_provider_adapter",
]
