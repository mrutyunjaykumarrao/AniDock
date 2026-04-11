from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from core.models import SearchResult


@dataclass(frozen=True)
class AnimeReference:
    title: str
    category_url: str


@dataclass(frozen=True)
class StreamPreferences:
    subtitle_mode: str | None = None
    prompt_for_mode: bool = True
    emit_logs: bool = True
    chooser: Callable[[int, str], int] | None = None


@dataclass(frozen=True)
class ResolvedStream:
    playlist_url: str
    subtitle_url: str | None
    subtitle_mode: str


@dataclass(frozen=True)
class ProviderCapabilities:
    subtitle_modes: tuple[str, ...]
    quality_selection: str
    notes: str | None = None
    supports_search: bool = True
    supports_episode_listing: bool = True
    supports_stream_resolution: bool = True
    supports_subtitle_preference: bool = True


class ProviderAdapter(ABC):
    key: str
    name: str
    capabilities: ProviderCapabilities

    @abstractmethod
    def search(self, query: str) -> list[SearchResult]:
        raise NotImplementedError

    @abstractmethod
    def list_episodes(self, anime_ref: AnimeReference | str) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def resolve_stream(self, episode_ref: str, preferences: StreamPreferences | None = None) -> ResolvedStream:
        raise NotImplementedError

    def available_subtitle_modes(self, episode_ref: str) -> tuple[str, ...]:
        return tuple(self.capabilities.subtitle_modes)
