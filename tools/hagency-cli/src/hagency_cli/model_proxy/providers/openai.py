from .base import PROTOCOL_RESPONSES, ProviderAdapter

ADAPTER = ProviderAdapter(
    name="openai",
    default_protocol=PROTOCOL_RESPONSES,
    default_base_url="https://api.openai.com/v1",
)
