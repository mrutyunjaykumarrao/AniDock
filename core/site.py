from dataclasses import dataclass
from typing import Callable

import requests

from core.exceptions import NoSearchResultsError, ProviderError
from core.models import SearchResult
from core.network import locked_print
from core.playlist import select_quality_playlist
from providers.base import StreamPreferences
from providers.registry import get_default_provider, get_provider


def _resolve_provider(provider_key: str | None = None):
    if provider_key:
        return get_provider(provider_key)
    return get_default_provider()


def anime_search_query(query_name: str, provider_key: str | None = None) -> list[SearchResult]:
    return _resolve_provider(provider_key).search(query_name)


def all_episode_of(anime_url: str, provider_key: str | None = None) -> list[str]:
    return _resolve_provider(provider_key).list_episodes(anime_url)


def resolve_stream_playlist(
    episode_url: str,
    preferred_mode: str | None = None,
    prompt_for_mode: bool = True,
    emit_logs: bool = True,
    chooser: Callable[[int, str], int] | None = None,
    provider_key: str | None = None,
) -> tuple[str, str | None, str]:
    resolved = _resolve_provider(provider_key).resolve_stream(
        episode_ref=episode_url,
        preferences=StreamPreferences(
            subtitle_mode=preferred_mode,
            prompt_for_mode=prompt_for_mode,
            emit_logs=emit_logs,
            chooser=chooser,
        ),
    )
    return resolved.playlist_url, resolved.subtitle_url, resolved.subtitle_mode


def get_available_subtitle_modes(episode_url: str, provider_key: str | None = None) -> tuple[str, ...]:
    return _resolve_provider(provider_key).available_subtitle_modes(episode_url)


@dataclass(frozen=True)
class PreparedStream:
    media_playlist_url: str
    subtitle_url: str | None
    subtitle_mode: str
    quality_label: str | None
    provider_key: str


def prepare_stream_with_fallback(
    episode_refs_by_provider: dict[str, str],
    provider_order: list[str],
    preferred_subtitle_mode: str | None,
    preferred_quality_label: str | None,
    prompt_for_subtitle_mode: bool,
    prompt_for_quality: bool,
    chooser: Callable[[int, str], int] | None = None,
    emit_logs: bool = True,
    emit_diagnostics: bool = True,
    diagnostics_callback: Callable[[str], None] | None = None,
) -> PreparedStream:
    def emit_diagnostic(message: str) -> None:
        if diagnostics_callback is not None:
            diagnostics_callback(message)
            return
        locked_print(message)

    attempted_failures: list[str] = []
    deduped_order: list[str] = []
    for provider_key in provider_order:
        if provider_key not in deduped_order:
            deduped_order.append(provider_key)

    for provider_key in deduped_order:
        episode_ref = episode_refs_by_provider.get(provider_key)
        if not episode_ref:
            failure_reason = "no episode reference available for this provider."
            attempted_failures.append("{0}: {1}".format(provider_key, failure_reason))
            if emit_diagnostics:
                message = "Provider attempt '{0}' failed: {1}".format(provider_key, failure_reason)
                emit_diagnostic(message)
            continue
        if emit_diagnostics:
            message = "Attempting provider '{0}' for stream preparation.".format(provider_key)
            emit_diagnostic(message)
        try:
            resolved = _resolve_provider(provider_key).resolve_stream(
                episode_ref=episode_ref,
                preferences=StreamPreferences(
                    subtitle_mode=preferred_subtitle_mode,
                    prompt_for_mode=prompt_for_subtitle_mode,
                    emit_logs=emit_logs,
                    chooser=chooser,
                ),
            )
            media_playlist_url, quality_label = select_quality_playlist(
                resolved.playlist_url,
                preferred_quality_label=preferred_quality_label,
                prompt_for_quality=prompt_for_quality,
                emit_logs=emit_logs,
                chooser=chooser,
            )
            if emit_diagnostics:
                if attempted_failures:
                    message = "Provider fallback selected: '{0}'.".format(provider_key)
                else:
                    message = "Provider '{0}' succeeded.".format(provider_key)
                emit_diagnostic(message)
            return PreparedStream(
                media_playlist_url=media_playlist_url,
                subtitle_url=resolved.subtitle_url,
                subtitle_mode=resolved.subtitle_mode,
                quality_label=quality_label,
                provider_key=provider_key,
            )
        except (NoSearchResultsError, ProviderError, requests.RequestException, OSError, ValueError) as exc:
            failure_reason = str(exc)
            attempted_failures.append("{0}: {1}".format(provider_key, failure_reason))
            if emit_diagnostics:
                message = "Provider attempt '{0}' failed: {1}".format(provider_key, failure_reason)
                emit_diagnostic(message)

    raise NoSearchResultsError(
        "Stream preparation failed for all providers. Attempts: {0}".format("; ".join(attempted_failures))
    )
