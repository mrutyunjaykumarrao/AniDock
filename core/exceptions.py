"""Custom exceptions used across AniDock."""


class InvalidAnimeNameError(ValueError):
    """Raised when the provided anime name is empty or invalid."""


class NoSearchResultsError(LookupError):
    """Raised when no anime matches the provided query."""


class ProviderError(RuntimeError):
    """Raised when a provider adapter fails to complete an operation."""


class ProviderUnavailableError(ProviderError):
    """Raised when a provider endpoint is unavailable or unreachable."""


class ProviderCapabilityError(ProviderError, NotImplementedError):
    """Raised when a provider does not support the requested capability."""


class ProviderParsingError(ProviderError):
    """Raised when provider responses cannot be parsed reliably."""
