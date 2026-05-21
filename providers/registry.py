from providers.base import ProviderAdapter

_PROVIDERS: dict[str, ProviderAdapter] = {}
_DEFAULT_PROVIDER_KEY: str | None = None
_BUILTINS_REGISTERED = False


def register_provider(provider: ProviderAdapter, *, is_default: bool = False) -> None:
    global _DEFAULT_PROVIDER_KEY
    _PROVIDERS[provider.key] = provider
    if is_default or _DEFAULT_PROVIDER_KEY is None:
        _DEFAULT_PROVIDER_KEY = provider.key


def register_builtin_providers() -> None:
    global _BUILTINS_REGISTERED
    if _BUILTINS_REGISTERED:
        return
    from providers.anineko import AninekoProvider
    from providers.hianime import HiAnimeProvider

    register_provider(AninekoProvider(), is_default=True)
    register_provider(HiAnimeProvider(), is_default=False)
    _BUILTINS_REGISTERED = True


def get_provider(key: str) -> ProviderAdapter:
    register_builtin_providers()
    try:
        return _PROVIDERS[key]
    except KeyError as exc:
        raise KeyError("Unknown provider key: {0}".format(key)) from exc


def get_default_provider() -> ProviderAdapter:
    register_builtin_providers()
    if _DEFAULT_PROVIDER_KEY is None:
        raise RuntimeError("No provider has been registered.")
    return _PROVIDERS[_DEFAULT_PROVIDER_KEY]


def list_providers() -> list[str]:
    register_builtin_providers()
    return sorted(_PROVIDERS.keys())
