# Provider adapters

This directory is the extension seam for upstream provider families. A provider
adapter owns stable provider facts: the default wire protocol, API root,
credential header format, and operation paths. Request forwarding, protocol
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
)
```

The loader discovers `providers/acme.py` from `adapter = "acme"`; no central
registry edit is required. Omit `default_base_url` when every deployment has a
different endpoint. An adapter may support both OpenAI wire protocols while a
configured provider instance selects exactly one native `protocol`.

Use provider configuration for deployment-specific URLs, headers, query values,
timeouts, and secrets. Use a trusted Hook for enterprise signing or a wire
dialect that needs code. Do not add model catalogs, aliases, or routing rules to
an adapter.
