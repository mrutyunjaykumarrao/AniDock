"""Provider adapters and registry for stream sources."""

from providers.base import (
    AnimeReference,
    ProviderAdapter,
    ProviderCapabilities,
    ResolvedStream,
    StreamPreferences,
)
from providers.registry import get_default_provider, get_provider, list_providers, register_provider

__all__ = [
    "AnimeReference",
    "ProviderAdapter",
    "ProviderCapabilities",
    "ResolvedStream",
    "StreamPreferences",
    "get_default_provider",
    "get_provider",
    "list_providers",
    "register_provider",
]
