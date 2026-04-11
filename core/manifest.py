import json
import os
import threading
import time

from core.input_parsing import sanitize_file_part


def make_manifest_path(output_dir: str, anime_title: str) -> str:
    return os.path.join(output_dir, ".{0}.download_manifest.json".format(sanitize_file_part(anime_title)))


def default_manifest(anime_title: str) -> dict:
    return {
        "version": 1,
        "anime_title": anime_title,
        "updated_at": int(time.time()),
        "queue": [],
        "status_by_episode": {},
        "completed_files": {},
        "failed_errors": {},
        "completed_segments": {},
        "settings": {},
    }


def load_manifest(manifest_path: str, anime_title: str) -> dict:
    if not os.path.exists(manifest_path):
        return default_manifest(anime_title)
    with open(manifest_path, "r", encoding="utf-8") as manifest_file:
        payload = json.load(manifest_file)
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Unsupported manifest format at {0}".format(manifest_path))
    payload.setdefault("anime_title", anime_title)
    payload.setdefault("queue", [])
    payload.setdefault("status_by_episode", {})
    payload.setdefault("completed_files", {})
    payload.setdefault("failed_errors", {})
    payload.setdefault("completed_segments", {})
    payload.setdefault("settings", {})
    return payload


def save_manifest(manifest_path: str, manifest_data: dict) -> None:
    manifest_data["updated_at"] = int(time.time())
    with open(manifest_path, "w", encoding="utf-8") as manifest_file:
        json.dump(manifest_data, manifest_file, indent=2, sort_keys=True)


def set_manifest_episode_status(
    manifest_path: str,
    manifest_data: dict,
    manifest_lock: threading.Lock,
    episode_number: int,
    status: str,
    error_message: str | None = None,
    final_file: str | None = None,
) -> None:
    with manifest_lock:
        manifest_data["status_by_episode"][str(episode_number)] = status
        if error_message:
            manifest_data["failed_errors"][str(episode_number)] = error_message
        else:
            manifest_data["failed_errors"].pop(str(episode_number), None)
        if final_file:
            manifest_data["completed_files"][str(episode_number)] = final_file
        save_manifest(manifest_path, manifest_data)


def mark_segment_complete(
    manifest_path: str,
    manifest_data: dict,
    manifest_lock: threading.Lock,
    episode_number: int,
    segment_index: int,
) -> None:
    with manifest_lock:
        key = str(episode_number)
        segment_values = manifest_data["completed_segments"].setdefault(key, [])
        if segment_index not in segment_values:
            segment_values.append(segment_index)
            segment_values.sort()
        save_manifest(manifest_path, manifest_data)


def get_manifest_completed_segments(manifest_data: dict, episode_number: int) -> set[int]:
    values = manifest_data.get("completed_segments", {}).get(str(episode_number), [])
    return {int(item) for item in values if isinstance(item, int)}

