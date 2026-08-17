from .base import PROTOCOL_CHAT, ProviderAdapter

ADAPTER = ProviderAdapter(
    name="openai_compatible",
    default_protocol=PROTOCOL_CHAT,
)
