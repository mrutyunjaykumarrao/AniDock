import os
import sys

from core.network import PRINT_LOCK


ANSI_RESET = "\033[0m"
ANSI_CYAN = "\033[36m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"


def _supports_color() -> bool:
    return sys.stdout.isatty() and os.getenv("NO_COLOR") is None


def _style(text: str, code: str) -> str:
    if not _supports_color():
        return text
    return "{0}{1}{2}".format(code, text, ANSI_RESET)


def summarize_episode_statuses(
    status_by_episode: dict[int, str],
) -> tuple[list[int], list[int], list[int], list[int], list[int]]:
    queued = sorted(ep for ep, status in status_by_episode.items() if status == "queued")
    running = sorted(ep for ep, status in status_by_episode.items() if status == "running")
    done = sorted(ep for ep, status in status_by_episode.items() if status == "done")
    failed = sorted(ep for ep, status in status_by_episode.items() if status == "failed")
    paused = sorted(ep for ep, status in status_by_episode.items() if status == "paused")
    return queued, running, done, failed, paused


class ProgressRenderer:
    def __init__(self, worker_count: int):
        self.worker_count = worker_count
        self.multi_line = worker_count > 1
        self.slot_by_episode: dict[str, int] = {}
        self.free_slots = list(range(worker_count))
        self.last_lines = ["Worker idle" for _ in range(worker_count)]
        self.render_ready = False

    def begin(self) -> None:
        if not self.multi_line:
            return
        with PRINT_LOCK:
            print("\nDownload progress:")
            for line in self.last_lines:
                print(line)
        self.render_ready = True

    def reserve_slot(self, episode_name: str) -> int:
        if not self.multi_line:
            return 0
        if episode_name in self.slot_by_episode:
            return self.slot_by_episode[episode_name]
        if not self.free_slots:
            return 0
        slot = self.free_slots.pop(0)
        self.slot_by_episode[episode_name] = slot
        return slot

    def release_slot(self, episode_name: str) -> None:
        if not self.multi_line:
            return
        slot = self.slot_by_episode.pop(episode_name, None)
        if slot is None:
            return
        self.last_lines[slot] = "Worker idle"
        self.free_slots.append(slot)
        self.free_slots.sort()
        self._render_slot(slot)

    def update(self, episode_name: str, line: str) -> None:
        if not self.multi_line:
            with PRINT_LOCK:
                print("\r{0}: {1}".format(episode_name, line), end="", flush=True)
            return
        slot = self.reserve_slot(episode_name)
        self.last_lines[slot] = "{0}: {1}".format(episode_name, line)
        self._render_slot(slot)

    def finish_inline(self) -> None:
        if self.multi_line:
            return
        with PRINT_LOCK:
            print()

    def _render_slot(self, slot: int) -> None:
        if not self.multi_line or not self.render_ready:
            return
        with PRINT_LOCK:
            lines_to_move_up = self.worker_count - slot
            print("\033[{0}A".format(lines_to_move_up), end="")
            print("\r\033[2K{0}".format(self.last_lines[slot]), end="")
            print("\033[{0}B".format(lines_to_move_up), end="", flush=True)


def format_progress_line(
    current: int,
    total: int,
    downloaded_bytes: int,
    elapsed_seconds: float,
    session_downloaded_bytes: int,
) -> str:
    bar_width = 30
    filled_width = int(bar_width * current / total)
    bar = "#" * filled_width + "-" * (bar_width - filled_width)
    percent = 100.0 * current / total
    downloaded_mb = downloaded_bytes / (1024 * 1024)
    speed_mbps = 0.0
    if elapsed_seconds > 0:
        speed_mbps = (session_downloaded_bytes / (1024 * 1024)) / elapsed_seconds
    bar_styled = _style(bar, ANSI_CYAN)
    percent_styled = _style("{0:6.2f}%".format(percent), ANSI_GREEN)
    speed_styled = _style("{0:.2f} MB/s".format(speed_mbps), ANSI_YELLOW)
    return "[{0}] {1} ({2}/{3}) {4:.2f} MB {5}".format(
        bar_styled, percent_styled, current, total, downloaded_mb, speed_styled
    )
