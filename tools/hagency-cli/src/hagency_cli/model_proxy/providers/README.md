# Provider adapters

This directory is the extension seam for upstream provider families. A provider
adapter owns stable provider facts: the default wire protocol, API root,
credential header format, operation paths, and model-list path. Request forwarding, protocol
conversion, retries, streaming, and model selection do not belong here.

To add a provider, create a lowercase module whose filename is the adapter name
and export one `ADAPTER` value:

```python
from .base import PROTOCOL_CHAT, ProviderAdapter

ADAPTER = ProviderAdapter(
    name="acme",
    default_protocol=PROTOCOL_CHAT,
    default_base_url="https://api.acme.example/v1",
    api_key_header="X-Acme-Key",
    api_key_prefix="",
    models_path="models",
)
```

The loader discovers `providers/acme.py` from `adapter = "acme"`; no central
registry edit is required. Omit `default_base_url` when every deployment has a
different endpoint. An adapter may support both OpenAI wire protocols while a
configured provider instance selects exactly one native `protocol`. Set
`models_path = None` only when the provider has no standard model-list endpoint;
a configured Hook can then implement `fetch_models(ctx)` instead. Its compact
`list[str]` result has no creation metadata, so synthesized OpenAI model records
use `created = 0` as a stable unknown value.

Use provider configuration for deployment-specific URLs, headers, query values,
timeouts, and secrets. Use a trusted Hook for enterprise signing or a wire
dialect that needs code. Do not add model catalogs, aliases, or routing rules to
an adapter.

Environment-backed provider values come from `.env` beside the model proxy
configuration and are overridden by the process environment. Hooks receive the
same merged values through the read-only `HookInit.env` mapping.
