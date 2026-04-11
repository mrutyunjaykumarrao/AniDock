from dataclasses import dataclass, field

from core.models import SearchResult


@dataclass
class QueueItem:
    episode_number: int
    episode_url: str
    status: str = "queued"
    progress_percent: int = 0
    speed_text: str = "-"


@dataclass
class AppState:
    search_query: str = ""
    search_results: list[SearchResult] = field(default_factory=list)
    selected_anime_title: str = ""
    queue_items: list[QueueItem] = field(default_factory=list)
    is_busy: bool = False
    is_queue_running: bool = False
    status_text: str = "Ready"
    overall_progress_percent: int = 0
    current_episode_text: str = "-"
    current_speed_text: str = "-"
    provider_key: str = "anitaku"
    fallback_behavior: str = "none"
    fallback_custom: str = ""
    subtitle_mode: str = "SUB"
    subtitle_output_mode: str = "separate"
    quality_label: str = ""
    worker_count: int = 1
    output_container_policy: str = "auto"
    reduced_motion: bool = False
    queue_counts: dict[str, int] = field(
        default_factory=lambda: {"queued": 0, "running": 0, "done": 0, "failed": 0, "paused": 0}
    )
