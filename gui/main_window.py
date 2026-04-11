import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRect, QSequentialAnimationGroup, QTimer, Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QCheckBox
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGraphicsOpacityEffect,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.exceptions import NoSearchResultsError, ProviderError
from core.disk_space import run_disk_space_preflight
from core.downloader import download_single_episode
from core.input_parsing import extract_episode_number_from_url, sanitize_file_part
from core.manifest import load_manifest, make_manifest_path, save_manifest
from core.models import DownloadPausedError
from core.progress import summarize_episode_statuses
from core.site import all_episode_of, anime_search_query
from gui.state import AppState, QueueItem
from gui.task_runner import TaskRunner
from providers.registry import get_default_provider, list_providers


class _GuiProgressRenderer:
    def __init__(self, progress_callback, total_items: int, progress_by_episode: dict[int, int], state_lock: threading.Lock):
        self._progress_callback = progress_callback
        self._total_items = max(1, total_items)
        self._progress_by_episode = progress_by_episode
        self._state_lock = state_lock

    def begin(self) -> None:
        return

    def reserve_slot(self, episode_name: str) -> int:
        return 0

    def release_slot(self, episode_name: str) -> None:
        return

    def finish_inline(self) -> None:
        return

    def update(self, episode_name: str, line: str) -> None:
        match_episode = re.search(r"Episode\s+(\d+)$", episode_name)
        if not match_episode:
            return
        episode_number = int(match_episode.group(1))

        percent_match = re.search(r"\]\s+(\d+\.\d+)%", line)
        speed_match = re.search(r"(\d+\.\d+)\s+MB/s$", line)
        episode_percent = int(float(percent_match.group(1))) if percent_match else 0
        speed_text = "{0} MB/s".format(speed_match.group(1)) if speed_match else "-"

        with self._state_lock:
            self._progress_by_episode[episode_number] = max(0, min(100, episode_percent))
            overall_percent = int(sum(self._progress_by_episode.values()) / self._total_items)

        self._progress_callback(
            {
                "type": "progress",
                "episode_number": episode_number,
                "episode_percent": max(0, min(100, episode_percent)),
                "speed_text": speed_text,
                "overall_percent": max(0, min(100, overall_percent)),
                "current_episode": episode_name,
            }
        )


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AniDock GUI")
        self.resize(1200, 760)

        default_provider_key = get_default_provider().key
        self._provider_keys = list_providers()
        self.state = AppState(provider_key=default_provider_key)
        self.task_runner = TaskRunner()
        self._pause_event = threading.Event()
        self._status_animation_group: QSequentialAnimationGroup | None = None
        self._progress_animation: QPropertyAnimation | None = None
        self._last_status_text = self.state.status_text
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(40)
        self._render_timer.timeout.connect(self._render_state)

        self._episode_url_map_all: dict[int, str] = {}
        self._manifest_pending_numbers: list[int] = []
        self._manifest_failed_numbers: list[int] = []
        self._queue_source_provider_key: str = default_provider_key

        self._build_ui()
        self._setup_feedback_widgets()
        self._render_state()

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search anime title...")
        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self._on_search_clicked)
        self.queue_selected_button = QPushButton("Queue Selected")
        self.queue_selected_button.clicked.connect(self._on_queue_selected_clicked)
        search_row.addWidget(self.search_input)
        search_row.addWidget(self.search_button)
        search_row.addWidget(self.queue_selected_button)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        search_group = QGroupBox("Search Results")
        search_layout = QVBoxLayout(search_group)
        self.search_results_list = QListWidget()
        search_layout.addWidget(self.search_results_list)

        queue_group = QGroupBox("Episode Queue")
        queue_layout = QVBoxLayout(queue_group)
        self.queue_table = QTableWidget(0, 4)
        self.queue_table.setHorizontalHeaderLabels(["Episode", "Status", "Progress", "Speed"])
        self.queue_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.queue_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        queue_layout.addWidget(self.queue_table)

        queue_controls = QHBoxLayout()
        self.start_queue_button = QPushButton("Start Queue")
        self.start_queue_button.clicked.connect(self._on_start_queue_clicked)
        self.pause_queue_button = QPushButton("Pause Queue")
        self.pause_queue_button.clicked.connect(self._on_pause_queue_clicked)
        self.start_new_queue_button = QPushButton("Start New Queue")
        self.start_new_queue_button.clicked.connect(self._on_start_new_queue_clicked)
        self.continue_pending_button = QPushButton("Continue Pending")
        self.continue_pending_button.clicked.connect(self._on_continue_pending_clicked)
        self.retry_failed_button = QPushButton("Retry Failed")
        self.retry_failed_button.clicked.connect(self._on_retry_failed_clicked)
        queue_controls.addWidget(self.start_queue_button)
        queue_controls.addWidget(self.pause_queue_button)
        queue_controls.addWidget(self.start_new_queue_button)
        queue_controls.addWidget(self.continue_pending_button)
        queue_controls.addWidget(self.retry_failed_button)
        queue_layout.addLayout(queue_controls)

        splitter.addWidget(search_group)
        splitter.addWidget(queue_group)
        splitter.setSizes([460, 740])

        bottom_row = QHBoxLayout()

        progress_group = QGroupBox("Progress")
        progress_layout = QVBoxLayout(progress_group)
        self.overall_progress_bar = QProgressBar()
        self.overall_progress_bar.setRange(0, 100)
        self.overall_progress_bar.setValue(0)
        self.queue_summary_label = QLabel("queued:0 running:0 done:0 failed:0 paused:0")
        self.current_episode_label = QLabel("Current episode: -")
        self.current_speed_label = QLabel("Speed: -")
        self.status_label = QLabel("Ready")
        progress_layout.addWidget(self.overall_progress_bar)
        progress_layout.addWidget(self.queue_summary_label)
        progress_layout.addWidget(self.current_episode_label)
        progress_layout.addWidget(self.current_speed_label)
        progress_layout.addWidget(self.status_label)

        settings_group = QGroupBox("Settings")
        settings_layout = QFormLayout(settings_group)

        self.provider_combo = QComboBox()
        self.provider_combo.addItems(self._provider_keys)
        self.provider_combo.setCurrentText(self.state.provider_key)

        self.fallback_behavior_combo = QComboBox()
        self.fallback_behavior_combo.addItem("None", "none")
        self.fallback_behavior_combo.addItem("Try all other providers", "all")
        self.fallback_behavior_combo.addItem("Custom provider order", "custom")
        self.fallback_behavior_combo.currentIndexChanged.connect(self._on_fallback_behavior_changed)

        self.fallback_custom_input = QLineEdit()
        self.fallback_custom_input.setPlaceholderText("e.g. hianime")
        self.fallback_custom_input.setEnabled(False)

        self.subtitle_mode_combo = QComboBox()
        self.subtitle_mode_combo.addItems(["SUB", "HSUB/RAW"])

        self.subtitle_output_combo = QComboBox()
        self.subtitle_output_combo.addItem("Separate .vtt", "separate")
        self.subtitle_output_combo.addItem("Mux into container", "mux")

        self.quality_input = QLineEdit()
        self.quality_input.setPlaceholderText("auto-best (leave empty) or e.g. 1080p")

        self.workers_combo = QComboBox()
        self.workers_combo.addItems(["1", "2", "3"])

        self.container_policy_combo = QComboBox()
        self.container_policy_combo.addItem("auto", "auto")
        self.container_policy_combo.addItem("mp4", "mp4")
        self.container_policy_combo.addItem("mkv", "mkv")
        self.container_policy_combo.addItem("ts", "ts")
        self.reduced_motion_checkbox = QCheckBox("Reduce animations")
        self.reduced_motion_checkbox.toggled.connect(self._on_reduced_motion_toggled)

        settings_layout.addRow("Provider:", self.provider_combo)
        settings_layout.addRow("Fallback:", self.fallback_behavior_combo)
        settings_layout.addRow("Fallback order:", self.fallback_custom_input)
        settings_layout.addRow("Subtitle mode:", self.subtitle_mode_combo)
        settings_layout.addRow("Subtitle output:", self.subtitle_output_combo)
        settings_layout.addRow("Quality:", self.quality_input)
        settings_layout.addRow("Workers:", self.workers_combo)
        settings_layout.addRow("Container policy:", self.container_policy_combo)
        settings_layout.addRow("Accessibility:", self.reduced_motion_checkbox)

        bottom_row.addWidget(progress_group)
        bottom_row.addWidget(settings_group)

        layout.addLayout(search_row)
        layout.addWidget(splitter)
        layout.addLayout(bottom_row)

    def _on_fallback_behavior_changed(self) -> None:
        selected_behavior = self.fallback_behavior_combo.currentData()
        self.fallback_custom_input.setEnabled(selected_behavior == "custom")

    def _on_reduced_motion_toggled(self, checked: bool) -> None:
        self.state.reduced_motion = checked
        if checked:
            if self._progress_animation is not None:
                self._progress_animation.stop()
            if self._status_animation_group is not None:
                self._status_animation_group.stop()
            self._notice_opacity_animation.stop()
            self._notice_opacity_effect.setOpacity(1.0)
        self._show_notice(
            "Reduced motion {0}.".format("enabled" if checked else "disabled"),
            level="info",
            timeout_ms=1800,
        )

    def _request_render(self, immediate: bool = False) -> None:
        if immediate:
            if self._render_timer.isActive():
                self._render_timer.stop()
            self._render_state()
            return
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _setup_feedback_widgets(self) -> None:
        self._status_opacity_effect = QGraphicsOpacityEffect(self.status_label)
        self.status_label.setGraphicsEffect(self._status_opacity_effect)
        self._status_opacity_effect.setOpacity(1.0)

        self.notice_label = QLabel(self)
        self.notice_label.setObjectName("toastNotice")
        self.notice_label.setWordWrap(True)
        self.notice_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.notice_label.hide()
        self.notice_label.setStyleSheet(
            "QLabel#toastNotice {"
            "background-color: rgba(26, 26, 26, 230);"
            "color: #f5f5f5;"
            "padding: 8px 12px;"
            "border-radius: 8px;"
            "}"
        )
        self._notice_opacity_effect = QGraphicsOpacityEffect(self.notice_label)
        self.notice_label.setGraphicsEffect(self._notice_opacity_effect)
        self._notice_opacity_effect.setOpacity(0.0)
        self._notice_opacity_animation = QPropertyAnimation(self._notice_opacity_effect, b"opacity", self)
        self._notice_opacity_animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._notice_opacity_animation.setDuration(180)
        self._notice_opacity_animation.finished.connect(self._on_notice_animation_finished)
        self._notice_hide_timer = QTimer(self)
        self._notice_hide_timer.setSingleShot(True)
        self._notice_hide_timer.timeout.connect(self._hide_notice)

    def _show_notice(self, message: str, level: str = "info", timeout_ms: int = 2600) -> None:
        if not message:
            return
        level_styles = {
            "info": "rgba(26, 26, 26, 230)",
            "warning": "rgba(144, 97, 16, 235)",
            "error": "rgba(152, 28, 44, 235)",
        }
        bg = level_styles.get(level, level_styles["info"])
        self.notice_label.setStyleSheet(
            "QLabel#toastNotice {"
            "background-color: {0};"
            "color: #f5f5f5;"
            "padding: 8px 12px;"
            "border-radius: 8px;"
            "}".format(bg)
        )
        self.notice_label.setText(message)
        self.notice_label.adjustSize()
        self._position_notice_label()
        self.notice_label.show()
        self.notice_label.raise_()

        self._notice_hide_timer.stop()
        self._notice_opacity_animation.stop()
        if self.state.reduced_motion:
            self._notice_opacity_effect.setOpacity(1.0)
        else:
            self._notice_opacity_effect.setOpacity(0.0)
            self._notice_opacity_animation.setStartValue(0.0)
            self._notice_opacity_animation.setEndValue(1.0)
            self._notice_opacity_animation.start()
        self._notice_hide_timer.start(timeout_ms)

    def _hide_notice(self) -> None:
        if self.state.reduced_motion:
            self.notice_label.hide()
            self._notice_opacity_effect.setOpacity(0.0)
            return
        self._notice_opacity_animation.stop()
        self._notice_opacity_animation.setStartValue(self._notice_opacity_effect.opacity())
        self._notice_opacity_animation.setEndValue(0.0)
        self._notice_opacity_animation.start()

    def _on_notice_animation_finished(self) -> None:
        if self._notice_opacity_effect.opacity() <= 0.05:
            self.notice_label.hide()

    def _position_notice_label(self) -> None:
        if not hasattr(self, "notice_label"):
            return
        margin = 18
        max_width = max(240, self.width() // 2)
        self.notice_label.setMaximumWidth(max_width)
        self.notice_label.adjustSize()
        target_width = min(max_width, self.notice_label.sizeHint().width())
        target_height = self.notice_label.sizeHint().height()
        x = max(margin, self.width() - target_width - margin)
        y = max(margin, self.height() - target_height - margin)
        self.notice_label.setGeometry(QRect(x, y, target_width, target_height))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "notice_label"):
            self._position_notice_label()

    def _sync_settings_state(self) -> None:
        self.state.provider_key = self.provider_combo.currentText().strip() or self.state.provider_key
        self.state.fallback_behavior = str(self.fallback_behavior_combo.currentData() or "none")
        self.state.fallback_custom = self.fallback_custom_input.text().strip()
        self.state.subtitle_mode = self.subtitle_mode_combo.currentText()
        self.state.subtitle_output_mode = str(self.subtitle_output_combo.currentData() or "separate")
        self.state.quality_label = self.quality_input.text().strip()
        self.state.worker_count = int(self.workers_combo.currentText())
        self.state.output_container_policy = str(self.container_policy_combo.currentData() or "auto")
        self.state.reduced_motion = self.reduced_motion_checkbox.isChecked()

    def _selected_fallback_provider_keys(self, primary_provider_key: str) -> list[str]:
        self._sync_settings_state()
        behavior = self.state.fallback_behavior
        if behavior == "none":
            return []
        if behavior == "all":
            return [key for key in self._provider_keys if key != primary_provider_key]
        requested = [item.strip() for item in self.state.fallback_custom.split(",") if item.strip()]
        if not requested:
            return []
        if len(set(requested)) != len(requested):
            raise ValueError("Fallback providers must be unique.")
        invalid = [provider_key for provider_key in requested if provider_key not in self._provider_keys]
        if invalid:
            raise ValueError("Unknown fallback provider(s): {0}".format(", ".join(invalid)))
        return [provider_key for provider_key in requested if provider_key != primary_provider_key]

    def _on_search_clicked(self) -> None:
        query = self.search_input.text().strip()
        if not query:
            self.state.status_text = "Enter an anime name to search."
            self._render_state()
            return

        self._sync_settings_state()
        self.state.search_query = query
        self.state.is_busy = True
        self.state.status_text = "Searching for '{0}' via {1}...".format(query, self.state.provider_key)
        self._render_state()

        self.task_runner.submit(
            anime_search_query,
            query,
            provider_key=self.state.provider_key,
            on_result=self._on_search_results,
            on_error=self._on_task_error,
            on_finished=self._on_search_finished,
        )

    def _on_queue_selected_clicked(self) -> None:
        selected_row = self.search_results_list.currentRow()
        if selected_row < 0 or selected_row >= len(self.state.search_results):
            self.state.status_text = "Select a search result to build the queue."
            self._render_state()
            return

        selected = self.state.search_results[selected_row]
        self._sync_settings_state()
        self.state.is_busy = True
        self.state.status_text = "Loading episode list for '{0}'...".format(selected.title)
        self._render_state()

        self.task_runner.submit(
            self._build_queue_for_result,
            selected.title,
            selected.category_url,
            self.state.provider_key,
            on_result=self._on_queue_loaded,
            on_error=self._on_task_error,
            on_finished=self._on_search_finished,
        )

    def _on_start_new_queue_clicked(self) -> None:
        if not self._episode_url_map_all:
            self.state.status_text = "Load a queue first to start a new queue."
            self._render_state()
            return
        self._apply_queue_by_numbers(sorted(self._episode_url_map_all))
        self.state.status_text = "Queue reset to current episode selection."
        self._render_state()

    def _on_continue_pending_clicked(self) -> None:
        if not self._manifest_pending_numbers:
            self.state.status_text = "No pending episodes in manifest."
            self._show_notice("No pending episodes found in manifest.", level="warning")
            self._render_state()
            return
        self._apply_queue_by_numbers(self._manifest_pending_numbers)
        self.state.status_text = "Loaded pending manifest episodes into queue."
        self._show_notice(
            "Loaded {0} pending episode(s) from manifest.".format(len(self.state.queue_items)),
            level="info",
        )
        self._render_state()

    def _on_retry_failed_clicked(self) -> None:
        if not self._manifest_failed_numbers:
            self.state.status_text = "No failed episodes in manifest."
            self._show_notice("No failed episodes to retry.", level="warning")
            self._render_state()
            return
        self._apply_queue_by_numbers(self._manifest_failed_numbers)
        self.state.status_text = "Loaded failed manifest episodes into queue."
        self._show_notice(
            "Retry queue loaded with {0} failed episode(s).".format(len(self.state.queue_items)),
            level="info",
        )
        self._render_state()

    def _on_start_queue_clicked(self) -> None:
        if self.state.is_queue_running:
            return
        if not self.state.queue_items:
            self.state.status_text = "Queue is empty. Add episodes from search results first."
            self._render_state()
            return

        self._sync_settings_state()
        try:
            fallback_provider_keys = self._selected_fallback_provider_keys(self.state.provider_key)
        except ValueError as exc:
            self.state.status_text = "Error: {0}".format(exc)
            self._show_notice(self.state.status_text, level="error", timeout_ms=3200)
            self._render_state()
            return

        queue_payload = [
            {"episode_number": item.episode_number, "episode_url": item.episode_url}
            for item in self.state.queue_items
        ]
        settings_payload = {
            "provider_key": self.state.provider_key,
            "fallback_provider_keys": fallback_provider_keys,
            "subtitle_mode": self.state.subtitle_mode,
            "subtitle_output_mode": self.state.subtitle_output_mode,
            "output_container_policy": self.state.output_container_policy,
            "quality_label": self.state.quality_label or None,
            "worker_count": self.state.worker_count,
            "source_provider_key": self._queue_source_provider_key,
        }

        self._pause_event.clear()
        self.state.is_queue_running = True
        self.state.status_text = "Queue started."
        if fallback_provider_keys:
            self._show_notice(
                "Fallback enabled: {0}".format(", ".join(fallback_provider_keys)),
                level="info",
                timeout_ms=3000,
            )
        self._render_state()

        self.task_runner.submit(
            self._run_queue_worker,
            self.state.selected_anime_title,
            queue_payload,
            settings_payload,
            on_progress=self._on_queue_progress,
            on_result=self._on_queue_result,
            on_error=self._on_task_error,
            on_finished=self._on_queue_finished,
        )

    def _on_pause_queue_clicked(self) -> None:
        if not self.state.is_queue_running:
            self.state.status_text = "Queue is not running."
            self._render_state()
            return
        self._pause_event.set()
        self.state.status_text = "Pause requested. Waiting for worker checkpoint..."
        self._render_state()

    def _on_search_results(self, results: object) -> None:
        self.state.search_results = list(results) if isinstance(results, list) else []
        count = len(self.state.search_results)
        self.state.status_text = "Found {0} result(s).".format(count)

    def _on_queue_loaded(self, payload: object) -> None:
        if not isinstance(payload, dict):
            self.state.queue_items = []
            self.state.status_text = "Unable to load queue."
            self._render_state()
            return

        title = str(payload.get("title", ""))
        source_provider_key = str(payload.get("provider_key", self.state.provider_key))
        episodes = payload.get("episodes", [])
        episode_url_map: dict[int, str] = {}
        for entry in episodes:
            if isinstance(entry, dict) and isinstance(entry.get("episode_number"), int) and isinstance(entry.get("episode_url"), str):
                episode_url_map[entry["episode_number"]] = entry["episode_url"]

        self.state.selected_anime_title = title
        self._queue_source_provider_key = source_provider_key
        self._episode_url_map_all = dict(sorted(episode_url_map.items()))
        self._apply_queue_by_numbers(sorted(self._episode_url_map_all))
        self._refresh_manifest_resume_options()

        self.state.overall_progress_percent = 0
        self.state.current_episode_text = "-"
        self.state.current_speed_text = "-"
        self.state.status_text = "Queued {0} episode(s) from '{1}'.".format(len(self.state.queue_items), title)

    def _apply_queue_by_numbers(self, episode_numbers: list[int]) -> None:
        self.state.queue_items = [
            QueueItem(episode_number=episode_number, episode_url=self._episode_url_map_all[episode_number])
            for episode_number in sorted(episode_numbers)
            if episode_number in self._episode_url_map_all
        ]
        self.state.overall_progress_percent = 0
        self.state.current_episode_text = "-"
        self.state.current_speed_text = "-"
        self._refresh_counts()

    def _refresh_manifest_resume_options(self) -> None:
        self._manifest_pending_numbers = []
        self._manifest_failed_numbers = []
        if not self.state.selected_anime_title or not self._episode_url_map_all:
            return

        output_dir = os.path.join("downloads", sanitize_file_part(self.state.selected_anime_title))
        manifest_path = make_manifest_path(output_dir, self.state.selected_anime_title)
        try:
            manifest_data = load_manifest(manifest_path, self.state.selected_anime_title)
        except (ValueError, OSError):
            return

        stored_queue = [int(ep) for ep in manifest_data.get("queue", []) if isinstance(ep, int)]
        statuses = manifest_data.get("status_by_episode", {})
        self._manifest_pending_numbers = [
            ep
            for ep in stored_queue
            if statuses.get(str(ep)) in ("queued", "running", "paused") and ep in self._episode_url_map_all
        ]
        self._manifest_failed_numbers = [
            ep for ep in stored_queue if statuses.get(str(ep)) == "failed" and ep in self._episode_url_map_all
        ]

    def _on_queue_progress(self, event: object) -> None:
        if not isinstance(event, dict):
            return

        event_type = event.get("type")
        if event_type == "status":
            episode_number = event.get("episode_number")
            status = event.get("status")
            if isinstance(episode_number, int) and isinstance(status, str):
                item = self._find_queue_item(episode_number)
                if item is not None:
                    item.status = status
                    if status in ("failed", "paused"):
                        item.speed_text = "-"
                    if status == "done":
                        item.progress_percent = 100
            message = event.get("message")
            if isinstance(message, str) and message:
                self.state.status_text = message
            overall_percent = event.get("overall_percent")
            if isinstance(overall_percent, int):
                self.state.overall_progress_percent = max(0, min(100, overall_percent))

        elif event_type == "progress":
            episode_number = event.get("episode_number")
            if isinstance(episode_number, int):
                item = self._find_queue_item(episode_number)
                if item is not None:
                    item.status = "running"
                    item.progress_percent = int(event.get("episode_percent", item.progress_percent))
                    speed_text = event.get("speed_text")
                    if isinstance(speed_text, str):
                        item.speed_text = speed_text
            overall_percent = event.get("overall_percent")
            if isinstance(overall_percent, int):
                self.state.overall_progress_percent = max(0, min(100, overall_percent))
            current_episode = event.get("current_episode")
            if isinstance(current_episode, str):
                self.state.current_episode_text = current_episode
            speed_text = event.get("speed_text")
            if isinstance(speed_text, str):
                self.state.current_speed_text = speed_text
        elif event_type == "notice":
            message = event.get("message")
            level = event.get("level", "info")
            if isinstance(message, str):
                self._show_notice(message, level=str(level))
            return

        self._refresh_counts()
        self._request_render()

    def _on_queue_result(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return

        status_map = payload.get("status_by_episode")
        if isinstance(status_map, dict):
            for episode_number, status in status_map.items():
                if isinstance(episode_number, int) and isinstance(status, str):
                    item = self._find_queue_item(episode_number)
                    if item is not None:
                        item.status = status
                        if status == "done":
                            item.progress_percent = 100
                        if status in ("failed", "paused"):
                            item.speed_text = "-"

        completed = int(payload.get("completed", 0))
        failed = int(payload.get("failed", 0))
        paused = int(payload.get("paused", 0))
        self.state.status_text = "Queue finished. done={0} failed={1} paused={2}".format(completed, failed, paused)
        self.state.overall_progress_percent = int(payload.get("overall_percent", self.state.overall_progress_percent))
        self._refresh_counts()
        self._refresh_manifest_resume_options()

    def _on_task_error(self, error_text: str) -> None:
        self.state.status_text = "Error: {0}".format(error_text)
        self._show_notice(self.state.status_text, level="error", timeout_ms=3400)

    def _on_search_finished(self) -> None:
        self.state.is_busy = False
        self._render_state()

    def _on_queue_finished(self) -> None:
        self.state.is_queue_running = False
        self._render_state()

    def _render_state(self) -> None:
        settings_enabled = not self.state.is_busy and not self.state.is_queue_running
        self.search_button.setEnabled(not self.state.is_busy)
        self.search_input.setEnabled(not self.state.is_busy)
        self.provider_combo.setEnabled(settings_enabled)
        self.fallback_behavior_combo.setEnabled(settings_enabled)
        self.fallback_custom_input.setEnabled(settings_enabled and self.fallback_behavior_combo.currentData() == "custom")
        self.subtitle_mode_combo.setEnabled(settings_enabled)
        self.subtitle_output_combo.setEnabled(settings_enabled)
        self.quality_input.setEnabled(settings_enabled)
        self.workers_combo.setEnabled(settings_enabled)
        self.container_policy_combo.setEnabled(settings_enabled)
        self.reduced_motion_checkbox.setEnabled(settings_enabled)
        self.reduced_motion_checkbox.blockSignals(True)
        self.reduced_motion_checkbox.setChecked(self.state.reduced_motion)
        self.reduced_motion_checkbox.blockSignals(False)

        self.queue_selected_button.setEnabled(not self.state.is_busy and not self.state.is_queue_running)
        self.start_queue_button.setEnabled(bool(self.state.queue_items) and not self.state.is_queue_running)
        self.pause_queue_button.setEnabled(self.state.is_queue_running)
        self.start_new_queue_button.setEnabled(bool(self._episode_url_map_all) and settings_enabled)
        self.continue_pending_button.setEnabled(bool(self._manifest_pending_numbers) and settings_enabled)
        self.retry_failed_button.setEnabled(bool(self._manifest_failed_numbers) and settings_enabled)

        self.search_results_list.clear()
        for result in self.state.search_results:
            self.search_results_list.addItem(result.title)

        self.queue_table.setRowCount(len(self.state.queue_items))
        for row, item in enumerate(self.state.queue_items):
            self.queue_table.setItem(row, 0, QTableWidgetItem(str(item.episode_number)))
            self.queue_table.setItem(row, 1, QTableWidgetItem(item.status))
            self.queue_table.setItem(row, 2, QTableWidgetItem("{0}%".format(item.progress_percent)))
            self.queue_table.setItem(row, 3, QTableWidgetItem(item.speed_text))

        self._set_animated_progress(self.state.overall_progress_percent)
        counts = self.state.queue_counts
        self.queue_summary_label.setText(
            "queued:{queued} running:{running} done:{done} failed:{failed} paused:{paused}".format(
                queued=counts["queued"],
                running=counts["running"],
                done=counts["done"],
                failed=counts["failed"],
                paused=counts["paused"],
            )
        )
        self.current_episode_label.setText("Current episode: {0}".format(self.state.current_episode_text))
        self.current_speed_label.setText("Speed: {0}".format(self.state.current_speed_text))
        if self._last_status_text != self.state.status_text:
            self.status_label.setText(self.state.status_text)
            self._animate_status_transition()
            self._last_status_text = self.state.status_text

    def _set_animated_progress(self, target_value: int) -> None:
        value = max(0, min(100, int(target_value)))
        if self.state.reduced_motion:
            if self._progress_animation is not None:
                self._progress_animation.stop()
            self.overall_progress_bar.setValue(value)
            return
        if self._progress_animation is None:
            self._progress_animation = QPropertyAnimation(self.overall_progress_bar, b"value", self)
            self._progress_animation.setDuration(180)
            self._progress_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._progress_animation.stop()
        self._progress_animation.setStartValue(self.overall_progress_bar.value())
        self._progress_animation.setEndValue(value)
        self._progress_animation.start()

    def _animate_status_transition(self) -> None:
        if self.state.reduced_motion:
            self._status_opacity_effect.setOpacity(1.0)
            return
        if self._status_animation_group is not None:
            self._status_animation_group.stop()
        fade_out = QPropertyAnimation(self._status_opacity_effect, b"opacity", self)
        fade_out.setDuration(80)
        fade_out.setStartValue(1.0)
        fade_out.setEndValue(0.45)
        fade_out.setEasingCurve(QEasingCurve.Type.InOutQuad)
        fade_in = QPropertyAnimation(self._status_opacity_effect, b"opacity", self)
        fade_in.setDuration(140)
        fade_in.setStartValue(0.45)
        fade_in.setEndValue(1.0)
        fade_in.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._status_animation_group = QSequentialAnimationGroup(self)
        self._status_animation_group.addAnimation(fade_out)
        self._status_animation_group.addAnimation(fade_in)
        self._status_animation_group.start()

    def _find_queue_item(self, episode_number: int) -> QueueItem | None:
        for item in self.state.queue_items:
            if item.episode_number == episode_number:
                return item
        return None

    def _refresh_counts(self) -> None:
        status_by_episode = {item.episode_number: item.status for item in self.state.queue_items}
        queued, running, done, failed, paused = summarize_episode_statuses(status_by_episode)
        self.state.queue_counts = {
            "queued": len(queued),
            "running": len(running),
            "done": len(done),
            "failed": len(failed),
            "paused": len(paused),
        }

    def _normalize_title(self, value: str) -> str:
        return "".join(character.lower() for character in value if character.isalnum())

    def _select_best_anime_result(self, results: list, anime_title: str):
        target = self._normalize_title(anime_title)
        for result in results:
            if self._normalize_title(result.title) == target:
                return result
        for result in results:
            normalized = self._normalize_title(result.title)
            if target and (target in normalized or normalized in target):
                return result
        return results[0]

    def _build_provider_episode_map(self, provider_key: str, anime_title: str) -> dict[int, str]:
        provider_results = anime_search_query(anime_title, provider_key=provider_key)
        selected_result = self._select_best_anime_result(provider_results, anime_title)
        provider_episode_links = all_episode_of(selected_result.category_url, provider_key=provider_key)
        episode_map: dict[int, str] = {}
        for episode_url in provider_episode_links:
            extracted_number = extract_episode_number_from_url(episode_url)
            if extracted_number is not None:
                episode_map[extracted_number] = episode_url
        return episode_map

    def _build_queue_for_result(self, title: str, category_url: str, provider_key: str) -> dict[str, object]:
        episode_links = all_episode_of(category_url, provider_key=provider_key)
        episode_number_to_url: dict[int, str] = {}
        for episode_url in episode_links:
            number = extract_episode_number_from_url(episode_url)
            if number is not None:
                episode_number_to_url[number] = episode_url

        episodes = [
            {"episode_number": episode_number, "episode_url": episode_number_to_url[episode_number]}
            for episode_number in sorted(episode_number_to_url)
        ]
        return {"title": title, "provider_key": provider_key, "episodes": episodes}

    def _run_queue_worker(
        self,
        anime_title: str,
        queue_payload: list[dict[str, object]],
        settings_payload: dict[str, object],
        progress_callback,
    ) -> dict[str, object]:
        def emit_notice(message: str, level: str = "info") -> None:
            progress_callback({"type": "notice", "level": level, "message": message})

        def emit_worker_diagnostic(message: str) -> None:
            lowered = message.lower()
            if "fallback selected" in lowered or "using fallback provider" in lowered:
                emit_notice(message, level="info")
            elif "retrying failed segments" in lowered:
                emit_notice(message, level="warning")

        selected_episode_numbers = [
            int(item["episode_number"])
            for item in queue_payload
            if isinstance(item.get("episode_number"), int)
        ]
        if not selected_episode_numbers:
            return {"status_by_episode": {}, "completed": 0, "failed": 0, "paused": 0, "overall_percent": 0}

        episode_number_to_url = {
            int(item["episode_number"]): str(item["episode_url"])
            for item in queue_payload
            if isinstance(item.get("episode_number"), int) and isinstance(item.get("episode_url"), str)
        }
        output_dir = os.path.join("downloads", sanitize_file_part(anime_title))
        os.makedirs(output_dir, exist_ok=True)
        manifest_path = make_manifest_path(output_dir, anime_title)
        manifest_lock = threading.Lock()
        manifest_data = load_manifest(manifest_path, anime_title)

        selected_provider_key = str(settings_payload.get("provider_key", get_default_provider().key))
        fallback_provider_keys = [
            provider_key
            for provider_key in settings_payload.get("fallback_provider_keys", [])
            if isinstance(provider_key, str) and provider_key != selected_provider_key
        ]
        provider_order = [selected_provider_key] + fallback_provider_keys
        if fallback_provider_keys:
            emit_notice("Provider fallback order: {0}".format(" -> ".join(provider_order)), level="info")

        preferred_subtitle_mode = str(settings_payload.get("subtitle_mode", "SUB"))
        subtitle_output_mode = str(settings_payload.get("subtitle_output_mode", "separate"))
        output_container_policy = str(settings_payload.get("output_container_policy", "auto"))
        preferred_quality_label = settings_payload.get("quality_label")
        if not isinstance(preferred_quality_label, str) or not preferred_quality_label.strip():
            preferred_quality_label = None
        worker_count = int(settings_payload.get("worker_count", 1))
        if worker_count not in (1, 2, 3):
            worker_count = 1
        source_provider_key = str(settings_payload.get("source_provider_key", selected_provider_key))

        with manifest_lock:
            manifest_data["queue"] = selected_episode_numbers
            manifest_data["settings"] = {
                "provider_key": selected_provider_key,
                "fallback_provider_keys": fallback_provider_keys,
                "subtitle_mode": preferred_subtitle_mode,
                "subtitle_output_mode": subtitle_output_mode,
                "output_container_policy": output_container_policy,
                "quality_label": preferred_quality_label,
                "worker_count": worker_count,
            }
            for episode_number in selected_episode_numbers:
                manifest_data["status_by_episode"][str(episode_number)] = "queued"
            save_manifest(manifest_path, manifest_data)

        provider_episode_maps: dict[str, dict[int, str]] = {}
        for provider_key in provider_order:
            if provider_key == source_provider_key:
                provider_episode_maps[provider_key] = episode_number_to_url
            else:
                provider_episode_maps[provider_key] = self._build_provider_episode_map(provider_key, anime_title)

        episode_provider_url_map: dict[int, dict[str, str]] = {}
        for episode_number in selected_episode_numbers:
            refs_by_provider: dict[str, str] = {}
            for provider_key in provider_order:
                episode_ref = provider_episode_maps.get(provider_key, {}).get(episode_number)
                if episode_ref:
                    refs_by_provider[provider_key] = episode_ref
            if source_provider_key not in refs_by_provider and episode_number in episode_number_to_url:
                refs_by_provider[source_provider_key] = episode_number_to_url[episode_number]
            episode_provider_url_map[episode_number] = refs_by_provider

        if not run_disk_space_preflight(
            episode_numbers=selected_episode_numbers,
            episode_provider_url_map=episode_provider_url_map,
            provider_order=provider_order,
            output_dir=output_dir,
            preferred_subtitle_mode=preferred_subtitle_mode,
            preferred_quality_label=preferred_quality_label,
            subtitle_output_mode=subtitle_output_mode,
            output_container_policy=output_container_policy,
            worker_count=worker_count,
            emit_logs=False,
        ):
            raise OSError("Insufficient disk space for queued download batch.")

        status_by_episode: dict[int, str] = {episode_number: "queued" for episode_number in selected_episode_numbers}
        progress_by_episode: dict[int, int] = {episode_number: 0 for episode_number in selected_episode_numbers}
        state_lock = threading.Lock()
        completed_count = 0
        failed_count = 0

        def update_episode_status(episode_number: int, status: str) -> None:
            nonlocal completed_count, failed_count
            with state_lock:
                status_by_episode[episode_number] = status
                if status == "done":
                    progress_by_episode[episode_number] = 100
                    completed_count = len([item for item in status_by_episode.values() if item == "done"])
                elif status in ("failed", "paused"):
                    progress_by_episode[episode_number] = 0
                    failed_count = len([item for item in status_by_episode.values() if item == "failed"])
                overall_percent = int(sum(progress_by_episode.values()) / max(1, len(progress_by_episode)))
            progress_callback(
                {
                    "type": "status",
                    "episode_number": episode_number,
                    "status": status,
                    "message": "Episode {0} {1}.".format(episode_number, status),
                    "overall_percent": max(0, min(100, overall_percent)),
                }
            )

        progress_renderer = _GuiProgressRenderer(
            progress_callback=progress_callback,
            total_items=len(selected_episode_numbers),
            progress_by_episode=progress_by_episode,
            state_lock=state_lock,
        )

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_to_episode = {
                executor.submit(
                    download_single_episode,
                    episode_number=episode_number,
                    anime_title=anime_title,
                    episode_url=episode_number_to_url[episode_number],
                    output_dir=output_dir,
                    preferred_subtitle_mode=preferred_subtitle_mode,
                    subtitle_output_mode=subtitle_output_mode,
                    output_container_policy=output_container_policy,  # type: ignore[arg-type]
                    preferred_quality_label=preferred_quality_label,
                    progress_renderer=progress_renderer,
                    update_episode_status=update_episode_status,
                    verbose=False,
                    pause_event=self._pause_event,
                    manifest_path=manifest_path,
                    manifest_data=manifest_data,
                    manifest_lock=manifest_lock,
                    provider_order=provider_order,
                    episode_refs_by_provider=episode_provider_url_map.get(episode_number, {}),
                    diagnostics_callback=emit_worker_diagnostic,
                ): episode_number
                for episode_number in selected_episode_numbers
            }
            for future in as_completed(future_to_episode):
                episode_number = future_to_episode[future]
                try:
                    future.result()
                except DownloadPausedError:
                    update_episode_status(episode_number, "paused")
                    emit_notice("Episode {0} paused by user.".format(episode_number), level="warning")
                except (NoSearchResultsError, ProviderError, requests.RequestException, OSError, ValueError) as exc:
                    update_episode_status(episode_number, "failed")
                    emit_notice("Episode {0} failed: {1}".format(episode_number, exc), level="error")

        with state_lock:
            completed_count = len([item for item in status_by_episode.values() if item == "done"])
            failed_count = len([item for item in status_by_episode.values() if item == "failed"])
            paused_count = len([item for item in status_by_episode.values() if item == "paused"])
            overall_percent = int(sum(progress_by_episode.values()) / max(1, len(progress_by_episode)))

        return {
            "status_by_episode": status_by_episode,
            "completed": completed_count,
            "failed": failed_count,
            "paused": paused_count,
            "overall_percent": max(0, min(100, overall_percent)),
        }
