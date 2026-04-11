#!/usr/bin/env python3

import os
import re
import shutil
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import requests

from core.exceptions import InvalidAnimeNameError, NoSearchResultsError, ProviderError
from core.downloader import download_single_episode
from core.input_parsing import (
    extract_episode_number_from_url,
    load_queue_selection_from_file,
    parse_episode_selection,
    sanitize_file_part,
)
from core.manifest import load_manifest, make_manifest_path, save_manifest
from core.models import DownloadPausedError, OutputContainerPolicy
from core.network import locked_print, set_retry_logging_enabled
from core.progress import ProgressRenderer, summarize_episode_statuses
from core.site import all_episode_of, anime_search_query, get_available_subtitle_modes, prepare_stream_with_fallback
from providers.registry import get_default_provider, list_providers


ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_MAGENTA = "\033[35m"
ANSI_CYAN = "\033[36m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"


def _supports_color() -> bool:
    return sys.stdout.isatty() and os.getenv("NO_COLOR") is None


def _style(text: str, *codes: str) -> str:
    if not _supports_color():
        return text
    return "".join(codes) + text + ANSI_RESET


def _terminal_width() -> int:
    return max(72, shutil.get_terminal_size(fallback=(100, 24)).columns)


def print_section(title: str) -> None:
    divider = "=" * _terminal_width()
    print("\n" + _style(divider, ANSI_CYAN))
    print(_style(title, ANSI_BOLD, ANSI_CYAN))
    print(_style(divider, ANSI_CYAN))


def print_startup_banner() -> None:
    banner_lines = [
        "  █████╗ ███╗   ██╗██╗██████╗  ██████╗  ██████╗██╗  ██╗",
        " ██╔══██╗████╗  ██║██║██╔══██╗██╔═══██╗██╔════╝██║ ██╔╝",
        " ███████║██╔██╗ ██║██║██║  ██║██║   ██║██║     █████╔╝ ",
        " ██╔══██║██║╚██╗██║██║██║  ██║██║   ██║██║     ██╔═██╗ ",
        " ██║  ██║██║ ╚████║██║██████╔╝╚██████╔╝╚██████╗██║  ██╗",
        " ╚═╝  ╚═╝╚═╝  ╚═══╝╚═╝╚═════╝  ╚═════╝  ╚═════╝╚═╝  ╚═╝",
        "",
        "Multi-Provider Anime Downloader • CLI",
    ]

    width = _terminal_width()
    divider = ("=" * int(width * 0.8)).center(width)

    print(_style("\n" + divider, ANSI_MAGENTA))
    for line in banner_lines:
        print(_style(line.center(width), ANSI_BOLD, ANSI_CYAN))
    print(_style(divider + "\n", ANSI_MAGENTA))

def select_from_indexed_prompt(max_value: int, prompt: str, allow_back: bool = False) -> int | None:
    while True:
        raw_choice = input(prompt).strip().lower()
        if allow_back and _is_back_command(raw_choice):
            print()
            return None
        if not raw_choice.isdigit():
            if allow_back:
                print("Please enter a valid number or type 'back'.")
            else:
                print("Please enter a valid number.")
            continue
        choice = int(raw_choice)
        if 1 <= choice <= max_value:
            print()
            return choice
        if allow_back:
            print("Please choose a number between 1 and {0}, or type 'back'.".format(max_value))
        else:
            print("Please choose a number between 1 and {0}.".format(max_value))


def print_queue_status_summary(status_by_episode: dict[int, str], detail: bool = False) -> None:
    queued, running, done, failed, paused = summarize_episode_statuses(status_by_episode)
    locked_print(
        _style(
            "Queue status -> queued:{0} running:{1} done:{2} failed:{3} paused:{4}".format(
                len(queued), len(running), len(done), len(failed), len(paused)
            ),
            ANSI_CYAN,
        )
    )
    if detail:
        locked_print(
            "  queued={0} running={1} done={2} failed={3} paused={4}".format(
                ", ".join(str(ep) for ep in queued) or "-",
                ", ".join(str(ep) for ep in running) or "-",
                ", ".join(str(ep) for ep in done) or "-",
                ", ".join(str(ep) for ep in failed) or "-",
                ", ".join(str(ep) for ep in paused) or "-",
            )
        )


def _normalize_title(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())


def _is_back_command(raw_value: str) -> bool:
    return raw_value in ("\x1b", "esc", "back", "b")


def _is_dub_title(title: str) -> bool:
    normalized = title.lower()
    return "(dub" in normalized or normalized.endswith(" dub") or " dub)" in normalized


def _audio_base_title(title: str) -> str:
    cleaned = re.sub(r"\(.*?dub.*?\)", "", title, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bdub\b", "", cleaned, flags=re.IGNORECASE)
    return _normalize_title(cleaned)


def choose_audio_variant(selected_anime, search_results: list):
    base_title = _audio_base_title(selected_anime.title)
    related = [item for item in search_results if _audio_base_title(item.title) == base_title]
    if len(related) < 2:
        return selected_anime

    sub_candidate = next((item for item in related if not _is_dub_title(item.title)), None)
    dub_candidate = next((item for item in related if _is_dub_title(item.title)), None)
    if not sub_candidate or not dub_candidate:
        return selected_anime

    print("\n" + _style("Audio options detected for this anime:", ANSI_BOLD))
    print(_style("[1] SUB / Original audio", ANSI_GREEN))
    print(_style("[2] DUB / English dub", ANSI_GREEN))
    print("Type 'back' to keep your originally selected result.")
    audio_choice = select_from_indexed_prompt(2, "Select audio (1-2): ", allow_back=True)
    if audio_choice == 1:
        print("Using SUB variant: {0}".format(sub_candidate.title))
        return sub_candidate
    if audio_choice == 2:
        print("Using DUB variant: {0}".format(dub_candidate.title))
        return dub_candidate
    return selected_anime


def resolve_episode_links_with_fallback(
    selected_anime,
    anime_query: str,
    preferred_provider_key: str,
) -> tuple[object, list[str], str]:
    attempted: list[str] = []
    provider_order = [preferred_provider_key] + default_fallback_chain(preferred_provider_key)
    for index, provider_key in enumerate(provider_order):
        try:
            if index == 0:
                provider_result = selected_anime
            else:
                fallback_results = anime_search_query(anime_query, provider_key=provider_key)
                provider_result = select_best_anime_result(fallback_results, selected_anime.title)
            episode_links = all_episode_of(provider_result.category_url, provider_key=provider_key)
            if index > 0:
                print(
                    "Primary provider '{0}' failed for episode listing. Switched to fallback '{1}'.".format(
                        preferred_provider_key, provider_key
                    )
                )
            return provider_result, episode_links, provider_key
        except (NoSearchResultsError, ProviderError, requests.RequestException, OSError, ValueError) as exc:
            attempted.append("{0}: {1}".format(provider_key, exc))
    raise NoSearchResultsError(
        "Episode listing failed across provider chain. Attempts: {0}".format("; ".join(attempted))
    )


def choose_provider(default_provider_key: str) -> str:
    provider_keys = list_providers()
    print("\n" + _style("Provider options:", ANSI_BOLD))
    for index, provider_key in enumerate(provider_keys, start=1):
        suffix = " (default)" if provider_key == default_provider_key else ""
        print("[{0}] {1}{2}".format(index, provider_key, suffix))
    while True:
        raw_choice = input(
            "Select primary provider (1-{0}, Enter for {1}): ".format(
                len(provider_keys), default_provider_key
            )
        ).strip()
        if not raw_choice:
            return default_provider_key
        if raw_choice.isdigit():
            choice = int(raw_choice)
            if 1 <= choice <= len(provider_keys):
                return provider_keys[choice - 1]
        print("Please enter a valid selection.")


def default_fallback_chain(primary_provider_key: str) -> list[str]:
    return [provider_key for provider_key in list_providers() if provider_key != primary_provider_key]


def select_best_anime_result(results: list, anime_title: str):
    target = _normalize_title(anime_title)
    for result in results:
        if _normalize_title(result.title) == target:
            return result
    for result in results:
        normalized = _normalize_title(result.title)
        if target and (target in normalized or normalized in target):
            return result
    return results[0]


def build_provider_episode_map(
    provider_key: str,
    anime_title: str,
    fallback_to_empty: bool = True,
    emit_error_logs: bool = True,
) -> dict[int, str]:
    try:
        provider_results = anime_search_query(anime_title, provider_key=provider_key)
        selected_result = select_best_anime_result(provider_results, anime_title)
        provider_episode_links = all_episode_of(selected_result.category_url, provider_key=provider_key)
        episode_map: dict[int, str] = {}
        for episode_url in provider_episode_links:
            extracted_number = extract_episode_number_from_url(episode_url)
            if extracted_number is not None:
                episode_map[extracted_number] = episode_url
        return episode_map
    except (NoSearchResultsError, ProviderError, requests.RequestException, OSError, ValueError) as exc:
        if emit_error_logs:
            locked_print(
                "Could not build episode map for provider '{0}': {1}".format(
                    provider_key, exc
                )
            )
        if fallback_to_empty:
            return {}
        raise


def choose_episode_selection(
    episode_number_to_url: dict[int, str],
    resumable_episodes: list[int] | None = None,
    failed_episodes: list[int] | None = None,
    manifest_path: str | None = None,
    manifest_data: dict | None = None,
    manifest_lock: threading.Lock | None = None,
    output_dir: str | None = None,
    anime_title: str | None = None,
    allow_back_to_anime: bool = True,
) -> list[int] | None:
    def has_completed_artifact(episode_number: int) -> bool:
        if manifest_data is not None:
            completed_file = manifest_data.get("completed_files", {}).get(str(episode_number))
            if isinstance(completed_file, str) and completed_file.strip():
                if os.path.exists(completed_file) or os.path.exists(os.path.abspath(completed_file)):
                    return True
        if output_dir and anime_title:
            episode_base = sanitize_file_part("{0} Episode {1}".format(anime_title, episode_number))
            for extension in (".mkv", ".mp4", ".ts", ".webm", ".m4v", ".mov", ".avi"):
                if os.path.exists(os.path.join(output_dir, episode_base + extension)):
                    return True
        return False

    def delete_episode_artifacts(selected_episodes: list[int]) -> None:
        if not (manifest_path and manifest_data is not None and manifest_lock and output_dir and anime_title):
            print("Cleanup is unavailable for this run.")
            return

        removed_files = 0
        removed_dirs = 0
        for episode_number in selected_episodes:
            episode_base = sanitize_file_part("{0} Episode {1}".format(anime_title, episode_number))
            candidate_paths = [
                os.path.join(output_dir, "{0}.ts".format(episode_base)),
                os.path.join(output_dir, "{0}.mp4".format(episode_base)),
                os.path.join(output_dir, "{0}.mkv".format(episode_base)),
                os.path.join(output_dir, "{0}.webm".format(episode_base)),
                os.path.join(output_dir, "{0}.m4v".format(episode_base)),
                os.path.join(output_dir, "{0}.mov".format(episode_base)),
                os.path.join(output_dir, "{0}.avi".format(episode_base)),
                os.path.join(output_dir, "{0}.ts.parts".format(episode_base)),
            ]
            with manifest_lock:
                completed_file = manifest_data.get("completed_files", {}).get(str(episode_number))
            if isinstance(completed_file, str) and completed_file.strip():
                candidate_paths.append(completed_file)

            seen_paths: set[str] = set()
            for artifact_path in candidate_paths:
                normalized = os.path.abspath(artifact_path)
                if normalized in seen_paths:
                    continue
                seen_paths.add(normalized)
                if os.path.isdir(normalized):
                    try:
                        shutil.rmtree(normalized)
                        removed_dirs += 1
                    except OSError as exc:
                        locked_print("Could not remove directory '{0}': {1}".format(normalized, exc))
                elif os.path.exists(normalized):
                    try:
                        os.remove(normalized)
                        removed_files += 1
                    except OSError as exc:
                        locked_print("Could not remove file '{0}': {1}".format(normalized, exc))

        with manifest_lock:
            queue_values = manifest_data.get("queue", [])
            manifest_data["queue"] = [
                ep for ep in queue_values if isinstance(ep, int) and ep not in set(selected_episodes)
            ]
            for episode_number in selected_episodes:
                episode_key = str(episode_number)
                manifest_data.get("status_by_episode", {}).pop(episode_key, None)
                manifest_data.get("failed_errors", {}).pop(episode_key, None)
                manifest_data.get("completed_segments", {}).pop(episode_key, None)
                manifest_data.get("completed_files", {}).pop(episode_key, None)
            save_manifest(manifest_path, manifest_data)

        print(
            "Cleanup completed for episodes: {0}. Removed files: {1}, removed partial directories: {2}.".format(
                ", ".join(str(ep) for ep in selected_episodes),
                removed_files,
                removed_dirs,
            )
        )

    max_episode = max(episode_number_to_url)
    min_episode = min(episode_number_to_url)
    available_numbers = set(episode_number_to_url)
    while True:
        resumable = [ep for ep in (resumable_episodes or []) if ep in available_numbers]
        failed = [ep for ep in (failed_episodes or []) if ep in available_numbers]
        if manifest_data is not None:
            stored_statuses = manifest_data.get("status_by_episode", {})
            status_episode_numbers = [int(key) for key in stored_statuses if str(key).isdigit()]
            stored_queue = [int(ep) for ep in manifest_data.get("queue", []) if isinstance(ep, int)]
            tracked_episodes = sorted(set(stored_queue + status_episode_numbers))

            reconciled = False
            for episode_number in tracked_episodes:
                if episode_number not in available_numbers:
                    continue
                if has_completed_artifact(episode_number):
                    if stored_statuses.get(str(episode_number)) != "done":
                        stored_statuses[str(episode_number)] = "done"
                        manifest_data.get("failed_errors", {}).pop(str(episode_number), None)
                        reconciled = True
            if reconciled and manifest_lock is not None and manifest_path:
                with manifest_lock:
                    save_manifest(manifest_path, manifest_data)

            resumable = []
            failed = []
            for episode_number in tracked_episodes:
                if episode_number not in available_numbers:
                    continue
                if has_completed_artifact(episode_number):
                    continue
                status = stored_statuses.get(str(episode_number))
                if status in ("queued", "running", "paused", "failed"):
                    resumable.append(episode_number)
                if status == "failed":
                    failed.append(episode_number)

        print_section("Episode Queue Setup")
        print(_style("[1] Enter episodes manually", ANSI_GREEN))
        print(_style("[2] Load episodes from queue file (.txt/.json)", ANSI_GREEN))
        next_choice = 3
        resume_choice: int | None = None
        retry_choice: int | None = None
        cleanup_choice: int | None = None
        if resumable:
            preview = ", ".join(str(ep) for ep in resumable[:8])
            suffix = "..." if len(resumable) > 8 else ""
            print(_style("[{0}] Resume unfinished episodes ({1}{2})".format(next_choice, preview, suffix), ANSI_GREEN))
            resume_choice = next_choice
            next_choice += 1
        if failed:
            preview = ", ".join(str(ep) for ep in failed[:8])
            suffix = "..." if len(failed) > 8 else ""
            print(_style("[{0}] Retry failed episodes ({1}{2})".format(next_choice, preview, suffix), ANSI_GREEN))
            retry_choice = next_choice
            next_choice += 1
        if manifest_path and manifest_data is not None and manifest_lock and output_dir and anime_title:
            print(_style("[{0}] Cleanup downloaded/failed/partial episode data".format(next_choice), ANSI_GREEN))
            cleanup_choice = next_choice
            next_choice += 1

        queue_mode = select_from_indexed_prompt(
            next_choice - 1,
            "Select queue input mode (1-{0}): ".format(next_choice - 1),
            allow_back=True,
        )
        if queue_mode is None:
            if allow_back_to_anime:
                return None
            continue

        if resume_choice is not None and queue_mode == resume_choice:
            print("Resuming episodes from manifest: {0}".format(", ".join(str(ep) for ep in resumable)))
            return resumable
        if retry_choice is not None and queue_mode == retry_choice:
            print("Retrying failed manifest episodes: {0}".format(", ".join(str(ep) for ep in failed)))
            return failed
        if cleanup_choice is not None and queue_mode == cleanup_choice:
            print("\nCleanup episode data:")
            def choose_targets_from_category(label: str, candidates: list[int]) -> list[int] | None:
                if not candidates:
                    print("No episodes currently tracked for '{0}'.".format(label))
                    return []
                print("{0} episodes: {1}".format(label, ", ".join(str(ep) for ep in candidates)))
                print(_style("[1] Delete selected episodes from this list", ANSI_GREEN))
                print(_style("[2] Delete all episodes from this list", ANSI_GREEN))
                print("Type 'back' to return to cleanup menu.")
                category_choice = select_from_indexed_prompt(2, "Select delete mode (1-2): ", allow_back=True)
                if category_choice is None:
                    return None
                if category_choice == 2:
                    return candidates
                while True:
                    raw_cleanup = input(
                        "Enter episodes to cleanup from this list (example: 12, 1-12, 1,7,20): "
                    ).strip()
                    if _is_back_command(raw_cleanup.lower()):
                        return None
                    try:
                        return parse_episode_selection(raw_cleanup, set(candidates))
                    except ValueError as exc:
                        print("{0} Allowed episodes: {1}".format(exc, ", ".join(str(ep) for ep in candidates)))

            completed_candidates = sorted(
                ep
                for ep, status in ((int(k), v) for k, v in manifest_data.get("status_by_episode", {}).items() if str(k).isdigit())
                if ep in available_numbers and status == "done"
            )
            partial_candidates = sorted(
                ep
                for ep, status in ((int(k), v) for k, v in manifest_data.get("status_by_episode", {}).items() if str(k).isdigit())
                if ep in available_numbers and status in ("queued", "running", "paused")
            )
            failed_candidates = sorted(
                ep
                for ep, status in ((int(k), v) for k, v in manifest_data.get("status_by_episode", {}).items() if str(k).isdigit())
                if ep in available_numbers and status == "failed"
            )
            partial_segment_candidates = sorted(
                int(key)
                for key, values in manifest_data.get("completed_segments", {}).items()
                if str(key).isdigit() and isinstance(values, list) and values and int(key) in available_numbers
            )
            completed_file_candidates = sorted(
                int(key)
                for key, value in manifest_data.get("completed_files", {}).items()
                if str(key).isdigit() and isinstance(value, str) and int(key) in available_numbers
            )
            completed_candidates = sorted(set(completed_candidates + completed_file_candidates))
            partial_candidates = sorted(set(partial_candidates + partial_segment_candidates) - set(completed_candidates))

            print(_style("[1] Cleanup completed episodes ({0})".format(len(completed_candidates)), ANSI_GREEN))
            print(_style("[2] Cleanup partial/paused episodes ({0})".format(len(partial_candidates)), ANSI_GREEN))
            print(_style("[3] Cleanup failed episodes ({0})".format(len(failed_candidates)), ANSI_GREEN))
            print(_style("[4] Cleanup custom episode list/range", ANSI_GREEN))
            print("Type 'back' to return to queue setup.")
            cleanup_mode = select_from_indexed_prompt(4, "Select cleanup mode (1-4): ", allow_back=True)
            if cleanup_mode is None:
                continue

            cleanup_targets: list[int] = []
            if cleanup_mode == 1:
                chosen_targets = choose_targets_from_category("Completed", completed_candidates)
                if chosen_targets is None:
                    continue
                cleanup_targets = chosen_targets
            elif cleanup_mode == 2:
                chosen_targets = choose_targets_from_category("Partial/paused", partial_candidates)
                if chosen_targets is None:
                    continue
                cleanup_targets = chosen_targets
            elif cleanup_mode == 3:
                chosen_targets = choose_targets_from_category("Failed", failed_candidates)
                if chosen_targets is None:
                    continue
                cleanup_targets = chosen_targets
            else:
                print("Type 'back' to return to cleanup menu.")
                while True:
                    raw_cleanup = input(
                        "Enter episodes to cleanup (example: 12, 1-12, 1,7,20): "
                    ).strip()
                    if _is_back_command(raw_cleanup.lower()):
                        cleanup_targets = []
                        break
                    try:
                        cleanup_targets = parse_episode_selection(raw_cleanup, available_numbers)
                        break
                    except ValueError as exc:
                        print(
                            "{0} Available range includes episode numbers from {1} to {2}.".format(
                                exc, min_episode, max_episode
                            )
                        )

            if not cleanup_targets:
                print("No matching episodes found for cleanup.")
                continue
            delete_episode_artifacts(cleanup_targets)
            continue

        if queue_mode == 2:
            print("Type 'back' to return to queue setup.")
            while True:
                queue_file_path = input("Enter queue file path (.txt/.json): ").strip()
                if _is_back_command(queue_file_path.lower()):
                    break
                try:
                    raw_from_file = load_queue_selection_from_file(queue_file_path)
                    selected_from_file = parse_episode_selection(raw_from_file, available_numbers)
                    print("Loaded queue from file: {0}".format(os.path.abspath(os.path.expanduser(queue_file_path))))
                    return selected_from_file
                except ValueError as exc:
                    print("Queue file error: {0}".format(exc))
            continue

        print("Type 'back' to return to queue setup.")
        while True:
            raw_episode_selection = input(
                "Enter episode number/range/list (example: 12, 1-12, 1,7,20): "
            ).strip()
            if _is_back_command(raw_episode_selection.lower()):
                break
            try:
                return parse_episode_selection(raw_episode_selection, available_numbers)
            except ValueError as exc:
                print(
                    "{0} Available range includes episode numbers from {1} to {2}.".format(
                        exc, min_episode, max_episode
                    )
                )
        continue


def main() -> None:
    print_startup_banner()
    default_provider_key = get_default_provider().key
    print_section("Provider Selection")
    discovery_provider_key = choose_provider(default_provider_key)

    while True:
        print_section("Anime Search")
        selected_anime = None
        while True:
            while True:
                anime_query = input("Enter anime name to search: ").strip()
                if _is_back_command(anime_query.lower()):
                    print()
                    print_section("Provider Selection")
                    discovery_provider_key = choose_provider(default_provider_key)
                    print_section("Anime Search")
                    continue
                if anime_query:
                    break
                print("Anime name cannot be empty. Please enter a title or press Ctrl+C to exit.")
            search_results = anime_search_query(anime_query, provider_key=discovery_provider_key)

            print("\nSearch Results:")
            shown_results = search_results[:20]
            for index, result in enumerate(shown_results, start=1):
                print("[{0}] {1}".format(index, result.title))
            print("Type 'esc' (or press Esc then Enter) to go back and search again.")

            while True:
                raw_choice = input("Select anime (1-{0}): ".format(len(shown_results))).strip().lower()
                if _is_back_command(raw_choice):
                    print()
                    print_section("Anime Search")
                    break
                if raw_choice.isdigit():
                    anime_choice = int(raw_choice)
                    if 1 <= anime_choice <= len(shown_results):
                        print()
                        selected_anime = shown_results[anime_choice - 1]
                        break
                print("Please choose a number between 1 and {0}, or type 'esc'.".format(len(shown_results)))
            if selected_anime is not None:
                break

        if selected_anime is None:
            raise NoSearchResultsError("No anime selection was made.")

        selected_anime = choose_audio_variant(selected_anime, search_results)
        selected_anime, episode_links, discovery_provider_key = resolve_episode_links_with_fallback(
            selected_anime=selected_anime,
            anime_query=anime_query,
            preferred_provider_key=discovery_provider_key,
        )
        print("\nTotal episodes found: {0}".format(len(episode_links)))
        print("First episode URL: {0}".format(episode_links[0]))
        print("Last episode URL: {0}".format(episode_links[-1]))

        episode_number_to_url: dict[int, str] = {}
        for episode_url in episode_links:
            extracted_number = extract_episode_number_from_url(episode_url)
            if extracted_number is not None:
                episode_number_to_url[extracted_number] = episode_url

        if not episode_number_to_url:
            raise NoSearchResultsError("Could not map episodes to numeric episode IDs.")

        output_dir = os.path.join("downloads", sanitize_file_part(selected_anime.title))
        os.makedirs(output_dir, exist_ok=True)
        manifest_path = make_manifest_path(output_dir, selected_anime.title)
        manifest_lock = threading.Lock()
        manifest_data = load_manifest(manifest_path, selected_anime.title)

        stored_queue = [int(ep) for ep in manifest_data.get("queue", []) if isinstance(ep, int)]
        stored_statuses = manifest_data.get("status_by_episode", {})
        resumable_episodes = [
            ep for ep in stored_queue if stored_statuses.get(str(ep)) in ("queued", "running", "paused", "failed")
        ]
        failed_manifest_episodes = [ep for ep in stored_queue if stored_statuses.get(str(ep)) == "failed"]

        selected_episode_numbers = choose_episode_selection(
            episode_number_to_url,
            resumable_episodes=resumable_episodes,
            failed_episodes=failed_manifest_episodes,
            manifest_path=manifest_path,
            manifest_data=manifest_data,
            manifest_lock=manifest_lock,
            output_dir=output_dir,
            anime_title=selected_anime.title,
            allow_back_to_anime=True,
        )
        if selected_episode_numbers is None:
            continue

        print("\nQueued episodes: {0}".format(", ".join(str(ep) for ep in selected_episode_numbers)))
        with manifest_lock:
            manifest_data["queue"] = selected_episode_numbers
            save_manifest(manifest_path, manifest_data)
        break

    settings_payload = manifest_data.get("settings", {})
    saved_subtitle_mode = settings_payload.get("subtitle_mode")
    if saved_subtitle_mode not in ("SUB", "DUB", "HSUB/RAW"):
        saved_subtitle_mode = None
    saved_quality_label = settings_payload.get("quality_label")
    if saved_quality_label is not None and not isinstance(saved_quality_label, str):
        saved_quality_label = None
    saved_subtitle_output_mode = settings_payload.get("subtitle_output_mode")
    if saved_subtitle_output_mode not in ("separate", "mux"):
        saved_subtitle_output_mode = None
    saved_output_container_policy = settings_payload.get("output_container_policy")
    if saved_output_container_policy not in ("auto", "mp4", "mkv", "ts"):
        saved_output_container_policy = None
    available_provider_keys = set(list_providers())
    saved_provider_key = settings_payload.get("provider_key")
    if not isinstance(saved_provider_key, str) or saved_provider_key not in available_provider_keys:
        saved_provider_key = None
    raw_saved_fallback_provider_keys = settings_payload.get("fallback_provider_keys")
    saved_fallback_provider_keys: list[str] | None = None
    if isinstance(raw_saved_fallback_provider_keys, list):
        parsed_fallback_keys = [
            item
            for item in raw_saved_fallback_provider_keys
            if isinstance(item, str) and item in available_provider_keys
        ]
        if len(parsed_fallback_keys) == len(set(parsed_fallback_keys)):
            saved_fallback_provider_keys = parsed_fallback_keys
    saved_worker_count = settings_payload.get("worker_count")
    if not isinstance(saved_worker_count, int) or saved_worker_count not in (1, 2, 3):
        saved_worker_count = None

    while True:
        use_saved_settings = False
        print_section("Download Settings")
        if saved_subtitle_mode and saved_worker_count and saved_subtitle_output_mode and saved_provider_key:
            print("Saved manifest settings found:")
            print(
                "provider={0}, fallback={1}, subtitle_mode={2}, subtitle_output={3}, quality={4}, output_container={5}, parallel_workers={6}".format(
                    saved_provider_key,
                    ", ".join(saved_fallback_provider_keys or []) or "-",
                    saved_subtitle_mode,
                    saved_subtitle_output_mode,
                    saved_quality_label or "auto-best",
                    saved_output_container_policy or "auto",
                    saved_worker_count,
                )
            )
            print("[1] Proceed with saved settings")
            print("[2] Change settings for this run")
            print("Type 'back' to re-open queue setup.")
            settings_choice = select_from_indexed_prompt(2, "Select settings mode (1-2): ", allow_back=True)
            if settings_choice is None:
                selected_episode_numbers = choose_episode_selection(
                    episode_number_to_url,
                    resumable_episodes=resumable_episodes,
                    failed_episodes=failed_manifest_episodes,
                    manifest_path=manifest_path,
                    manifest_data=manifest_data,
                    manifest_lock=manifest_lock,
                    output_dir=output_dir,
                    anime_title=selected_anime.title,
                    allow_back_to_anime=False,
                )
                print("\nQueued episodes: {0}".format(", ".join(str(ep) for ep in selected_episode_numbers)))
                with manifest_lock:
                    manifest_data["queue"] = selected_episode_numbers
                    save_manifest(manifest_path, manifest_data)
                continue
            use_saved_settings = settings_choice == 1

        preferred_subtitle_mode = "SUB"
        subtitle_output_mode = "separate"
        output_container_policy: OutputContainerPolicy = "auto"
        preferred_quality_label: str | None = None
        selected_provider_key = discovery_provider_key
        fallback_provider_keys: list[str] = []
        worker_count = 1
        if use_saved_settings:
            preferred_subtitle_mode = saved_subtitle_mode or "SUB"
            subtitle_output_mode = saved_subtitle_output_mode or "separate"
            output_container_policy = saved_output_container_policy or "auto"
            preferred_quality_label = saved_quality_label
            selected_provider_key = saved_provider_key or discovery_provider_key
            fallback_provider_keys = [
                provider_key
                for provider_key in (saved_fallback_provider_keys or [])
                if provider_key != selected_provider_key
            ]
            if not fallback_provider_keys:
                fallback_provider_keys = default_fallback_chain(selected_provider_key)
            worker_count = saved_worker_count or 1
            print(
                "Applying saved provider chain: {0}".format(
                    " -> ".join([selected_provider_key] + fallback_provider_keys)
                )
            )
            if preferred_quality_label:
                print("Applying saved quality '{0}' to all queued episodes.".format(preferred_quality_label))
            else:
                print("Applying saved quality policy: auto-best per episode.")
            print("Applying saved subtitle mode '{0}' to all queued episodes.".format(preferred_subtitle_mode))
            print("Applying saved subtitle output mode '{0}'.".format(subtitle_output_mode))
            print("Applying saved output container policy '{0}'.".format(output_container_policy))
            print("Using saved parallel workers: {0}.".format(worker_count))
        else:
            selected_provider_key = discovery_provider_key
            fallback_provider_keys = default_fallback_chain(selected_provider_key)
            print("\nResolving stream preferences from queued episodes...")
            print(
                "Selected provider chain: {0}".format(
                    " -> ".join([selected_provider_key] + fallback_provider_keys)
                )
            )
            detected_subtitle_modes: tuple[str, ...] = ()
            for selected_episode_number in selected_episode_numbers[:3]:
                probe_episode_url = episode_number_to_url.get(selected_episode_number)
                if not probe_episode_url:
                    continue
                try:
                    detected_subtitle_modes = get_available_subtitle_modes(
                        probe_episode_url,
                        provider_key=selected_provider_key,
                    )
                except (NoSearchResultsError, ProviderError, requests.RequestException, OSError, ValueError):
                    continue
                if detected_subtitle_modes:
                    break

            if len(detected_subtitle_modes) > 1:
                mode_labels = {
                    "SUB": "SUB (Japanese audio + subtitles)",
                    "DUB": "DUB (dubbed audio when available)",
                    "HSUB/RAW": "HSUB/RAW (as provided by source)",
                }
                print("\n" + _style("Audio/stream mode options:", ANSI_BOLD))
                for index, mode in enumerate(detected_subtitle_modes, start=1):
                    print(_style("[{0}] {1}".format(index, mode_labels.get(mode, mode)), ANSI_GREEN))
                audio_mode_choice = select_from_indexed_prompt(
                    len(detected_subtitle_modes),
                    "Select mode (1-{0}): ".format(len(detected_subtitle_modes)),
                    allow_back=True,
                )
                if audio_mode_choice is None:
                    continue
                preferred_subtitle_mode = detected_subtitle_modes[audio_mode_choice - 1]
            elif len(detected_subtitle_modes) == 1:
                preferred_subtitle_mode = detected_subtitle_modes[0]
                print("Audio mode detected: '{0}'.".format(preferred_subtitle_mode))
            else:
                print("Audio mode could not be detected up front; defaulting to '{0}'.".format(preferred_subtitle_mode))

            print("Applying subtitle mode '{0}' to all queued episodes.".format(preferred_subtitle_mode))
            print("\n" + _style("Subtitle output options:", ANSI_BOLD))
            print(_style("[1] Keep subtitle as separate .vtt file (recommended)", ANSI_GREEN))
            print(_style("[2] Embed subtitle track into video container when possible", ANSI_GREEN))
            print("Type 'back' to restart Download Settings.")
            subtitle_output_choice = select_from_indexed_prompt(2, "Select subtitle output (1-2): ", allow_back=True)
            if subtitle_output_choice is None:
                continue
            subtitle_output_mode = "separate" if subtitle_output_choice == 1 else "mux"
            print("Applying subtitle output mode '{0}'.".format(subtitle_output_mode))

            print("\n" + _style("Output container policy:", ANSI_BOLD))
            print(_style("[1] auto (try .mp4, then .mkv, else keep .ts)", ANSI_GREEN))
            print(_style("[2] mp4 (try .mp4 only, else keep .ts)", ANSI_GREEN))
            print(_style("[3] mkv (try .mkv only, else keep .ts)", ANSI_GREEN))
            print(_style("[4] ts (skip remux and keep .ts)", ANSI_GREEN))
            container_choice = select_from_indexed_prompt(4, "Select output container policy (1-4): ", allow_back=True)
            if container_choice is None:
                continue
            output_container_policy = {
                1: "auto",
                2: "mp4",
                3: "mkv",
                4: "ts",
            }[container_choice]
            print("Applying output container policy '{0}'.".format(output_container_policy))

        break

    provider_order = [selected_provider_key] + [
        provider_key for provider_key in fallback_provider_keys if provider_key != selected_provider_key
    ]
    provider_episode_maps: dict[str, dict[int, str]] = {}
    for provider_key in provider_order:
        if provider_key == discovery_provider_key:
            provider_episode_maps[provider_key] = episode_number_to_url
        else:
            provider_episode_maps[provider_key] = build_provider_episode_map(
                provider_key,
                selected_anime.title,
                fallback_to_empty=True,
                emit_error_logs=False,
            )

    episode_provider_url_map: dict[int, dict[str, str]] = {}
    for episode_number in selected_episode_numbers:
        refs: dict[str, str] = {}
        for provider_key in provider_order:
            episode_ref = provider_episode_maps.get(provider_key, {}).get(episode_number)
            if episode_ref:
                refs[provider_key] = episode_ref
        episode_provider_url_map[episode_number] = refs

    if not use_saved_settings:
        preference_probe_candidates = selected_episode_numbers[:3]
        for selected_episode_number in preference_probe_candidates:
            try:
                prepared_stream = prepare_stream_with_fallback(
                    episode_refs_by_provider=episode_provider_url_map.get(selected_episode_number, {}),
                    provider_order=provider_order,
                    preferred_subtitle_mode=preferred_subtitle_mode,
                    preferred_quality_label=None,
                    prompt_for_subtitle_mode=False,
                    prompt_for_quality=True,
                    chooser=select_from_indexed_prompt,
                    emit_logs=True,
                    emit_diagnostics=False,
                )
                preferred_quality_label = prepared_stream.quality_label
                break
            except (NoSearchResultsError, ProviderError, requests.RequestException, OSError, ValueError) as exc:
                print(
                    "Could not derive batch preferences from episode {0}: {1}".format(
                        selected_episode_number, exc
                    )
                )
        else:
            print(
                "No queued episode could be resolved from initial probes. "
                "Will continue with fallback defaults and try each episode."
            )

        if preferred_quality_label:
            print("Applying quality '{0}' to all queued episodes.".format(preferred_quality_label))
        worker_count = 1
        if len(selected_episode_numbers) > 1:
            print("\n" + _style("Parallel download options:", ANSI_BOLD))
            print(_style("[1] 1 episode at a time", ANSI_GREEN))
            print(_style("[2] 2 episodes in parallel", ANSI_GREEN))
            print(_style("[3] 3 episodes in parallel", ANSI_GREEN))
            worker_choice = select_from_indexed_prompt(3, "Select parallel downloads (1-3): ", allow_back=False)
            if worker_choice is not None:
                worker_count = worker_choice
        print("Using {0} parallel worker(s).".format(worker_count))

    with manifest_lock:
        manifest_data["settings"] = {
            "provider_key": selected_provider_key,
            "fallback_provider_keys": fallback_provider_keys,
            "subtitle_mode": preferred_subtitle_mode,
            "subtitle_output_mode": subtitle_output_mode,
            "output_container_policy": output_container_policy,
            "quality_label": preferred_quality_label,
            "worker_count": worker_count,
        }
        save_manifest(manifest_path, manifest_data)

    completed_episodes: list[int] = []
    failed_episodes: list[int] = []
    status_by_episode: dict[int, str] = {episode_number: "queued" for episode_number in selected_episode_numbers}
    status_lock = threading.Lock()
    progress_renderer = ProgressRenderer(worker_count)
    with manifest_lock:
        for episode_number in selected_episode_numbers:
            manifest_data["status_by_episode"][str(episode_number)] = "queued"
        save_manifest(manifest_path, manifest_data)

    def update_episode_status(episode_number: int, status: str) -> None:
        with status_lock:
            status_by_episode[episode_number] = status
            snapshot = dict(status_by_episode)
        print_queue_status_summary(snapshot, detail=False)

    print_section("Download Run")
    print_queue_status_summary(status_by_episode, detail=True)
    progress_renderer.begin()
    pause_event = threading.Event()
    interrupted = False

    set_retry_logging_enabled(worker_count == 1)
    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            pending_episodes = list(selected_episode_numbers)
            running_futures: dict = {}

            def submit_next_jobs() -> None:
                while (not pause_event.is_set()) and pending_episodes and len(running_futures) < worker_count:
                    selected_episode_number = pending_episodes.pop(0)
                    future = executor.submit(
                        download_single_episode,
                        episode_number=selected_episode_number,
                        anime_title=selected_anime.title,
                        episode_url=episode_number_to_url[selected_episode_number],
                        output_dir=output_dir,
                        preferred_subtitle_mode=preferred_subtitle_mode,
                        subtitle_output_mode=subtitle_output_mode,
                        output_container_policy=output_container_policy,
                        preferred_quality_label=preferred_quality_label,
                        progress_renderer=progress_renderer,
                        update_episode_status=update_episode_status,
                        verbose=(worker_count == 1),
                        pause_event=pause_event,
                        manifest_path=manifest_path,
                        manifest_data=manifest_data,
                        manifest_lock=manifest_lock,
                        provider_order=provider_order,
                        episode_refs_by_provider=episode_provider_url_map.get(selected_episode_number, {}),
                    )
                    running_futures[future] = selected_episode_number

            submit_next_jobs()
            try:
                while running_futures:
                    done, _ = wait(set(running_futures), return_when=FIRST_COMPLETED)
                    for future in done:
                        episode_number = running_futures.pop(future)
                        try:
                            completed_episode, _ = future.result()
                            completed_episodes.append(completed_episode)
                            update_episode_status(completed_episode, "done")
                        except DownloadPausedError:
                            update_episode_status(episode_number, "paused")
                        except (NoSearchResultsError, ProviderError, requests.RequestException, OSError, ValueError) as exc:
                            locked_print("Episode {0} failed: {1}".format(episode_number, exc))
                            failed_episodes.append(episode_number)
                            update_episode_status(episode_number, "failed")
                    submit_next_jobs()
            except KeyboardInterrupt:
                interrupted = True
                pause_event.set()
                locked_print("\nPause requested. Finishing active segment writes and saving manifest...")
                for queued_episode in pending_episodes:
                    update_episode_status(queued_episode, "paused")
                while running_futures:
                    done, _ = wait(set(running_futures), return_when=FIRST_COMPLETED)
                    for future in done:
                        episode_number = running_futures.pop(future)
                        try:
                            completed_episode, _ = future.result()
                            completed_episodes.append(completed_episode)
                            update_episode_status(completed_episode, "done")
                        except DownloadPausedError:
                            update_episode_status(episode_number, "paused")
                        except (NoSearchResultsError, ProviderError, requests.RequestException, OSError, ValueError) as exc:
                            locked_print("Episode {0} failed: {1}".format(episode_number, exc))
                            failed_episodes.append(episode_number)
                            update_episode_status(episode_number, "failed")
                for episode_number, status in list(status_by_episode.items()):
                    if status == "running":
                        update_episode_status(episode_number, "paused")
    finally:
        set_retry_logging_enabled(True)

    print_queue_status_summary(status_by_episode, detail=True)

    print_section("Batch Summary")
    if interrupted:
        locked_print(
            _style(
                "Batch paused. Completed: {0}. Failed: {1}. Resume later using manifest mode.".format(
                    len(completed_episodes), len(failed_episodes)
                ),
                ANSI_YELLOW,
            )
        )
    else:
        print(
            _style(
                "Batch finished. Completed: {0}. Failed: {1}.".format(len(completed_episodes), len(failed_episodes)),
                ANSI_GREEN if not failed_episodes else ANSI_YELLOW,
            )
        )
    if completed_episodes:
        completed_episodes.sort()
        print(_style("Completed episodes: {0}".format(", ".join(str(ep) for ep in completed_episodes)), ANSI_GREEN))
    if failed_episodes:
        failed_episodes.sort()
        print(_style("Failed episodes: {0}".format(", ".join(str(ep) for ep in failed_episodes)), ANSI_YELLOW))
    paused_episodes = sorted(ep for ep, status in status_by_episode.items() if status == "paused")
    if paused_episodes:
        print(_style("Paused episodes: {0}".format(", ".join(str(ep) for ep in paused_episodes)), ANSI_YELLOW))


if __name__ == "__main__":
    try:
        main()
    except (
        InvalidAnimeNameError,
        NoSearchResultsError,
        ProviderError,
        requests.RequestException,
        OSError,
        ValueError,
        DownloadPausedError,
    ) as exc:
        print("Error: {0}".format(exc))
