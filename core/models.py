from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SearchResult:
    title: str
    category_url: str


@dataclass(frozen=True)
class PlaylistVariant:
    quality_label: str
    bandwidth: int
    playlist_url: str


@dataclass(frozen=True)
class ServerOption:
    label: str
    embed_url: str
    subtitle_url: str | None
    provider: str
    subtitle_mode: str


class DownloadPausedError(Exception):
    pass


OutputContainerPolicy = Literal["auto", "mp4", "mkv", "ts"]
