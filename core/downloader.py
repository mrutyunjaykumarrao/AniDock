import os
import shutil
import threading
import time
from typing import Callable

import requests

from core.exceptions import NoSearchResultsError, ProviderError
from core.manifest import (
    get_manifest_completed_segments,
    mark_segment_complete,
    set_manifest_episode_status,
)
from core.media import convert_ts_to_video, download_subtitle_file, mux_subtitle_into_video
from core.models import DownloadPausedError, OutputContainerPolicy
from core.network import MAX_FETCH_RETRIES, fetch, locked_print
from core.playlist import iter_segments
from core.progress import ProgressRenderer, format_progress_line
from core.site import prepare_stream_with_fallback
from core.input_parsing import sanitize_file_part


def download_hls_episode(
    episode_number: int,
    episode_name: str,
    media_playlist_url: str,
    output_dir: str,
    progress_renderer: ProgressRenderer,
    verbose: bool,
    pause_event: threading.Event,
    manifest_path: str,
    manifest_data: dict,
    manifest_lock: threading.Lock,
    diagnostics_callback: Callable[[str], None] | None = None,
) -> str:
    segments = list(iter_segments(media_playlist_url))
    if not segments:
        raise NoSearchResultsError("No media segments found in playlist: {0}".format(media_playlist_url))

    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, sanitize_file_part(episode_name) + ".ts")
    parts_dir = output_path + ".parts"
    os.makedirs(parts_dir, exist_ok=True)
    if verbose:
        locked_print("Downloading {0} segments to {1}".format(len(segments), output_path))

    completed_segments = get_manifest_completed_segments(manifest_data, episode_number)
    # Keep only segment markers that still have a corresponding on-disk part file.
    # This prevents stale manifest state from skipping required downloads.
    valid_completed_segments: set[int] = set()
    downloaded_bytes = 0
    for index in completed_segments:
        part_path = os.path.join(parts_dir, "{0:05d}.ts".format(index))
        if os.path.exists(part_path):
            valid_completed_segments.add(index)
            downloaded_bytes += os.path.getsize(part_path)
    completed_segments = valid_completed_segments

    pending_indices = [idx for idx in range(1, len(segments) + 1) if idx not in completed_segments]
    failed_indices: set[int] = set()
    started_at = time.time()
    session_downloaded_bytes = 0

    for idx in pending_indices:
        if pause_event.is_set():
            raise DownloadPausedError("Pause requested by user.")
        segment_url = segments[idx - 1]
        part_path = os.path.join(parts_dir, "{0:05d}.ts".format(idx))
        try:
            segment_response = fetch(segment_url)
            with open(part_path, "wb") as segment_file:
                segment_file.write(segment_response.content)
            downloaded_bytes += len(segment_response.content)
            session_downloaded_bytes += len(segment_response.content)
            completed_segments.add(idx)
            mark_segment_complete(
                manifest_path=manifest_path,
                manifest_data=manifest_data,
                manifest_lock=manifest_lock,
                episode_number=episode_number,
                segment_index=idx,
            )
        except requests.RequestException:
            failed_indices.add(idx)
        finally:
            progress_renderer.update(
                episode_name=episode_name,
                line=format_progress_line(
                    idx,
                    len(segments),
                    downloaded_bytes,
                    time.time() - started_at,
                    session_downloaded_bytes,
                ),
            )

    for retry_round in range(1, MAX_FETCH_RETRIES + 1):
        if not failed_indices:
            break
        if pause_event.is_set():
            raise DownloadPausedError("Pause requested by user.")
        if verbose:
            message = "Retrying failed segments for {0} (round {1}/{2}): {3}".format(
                episode_name,
                retry_round,
                MAX_FETCH_RETRIES,
                ", ".join(str(i) for i in sorted(failed_indices)),
            )
            locked_print(message)
            if diagnostics_callback is not None:
                diagnostics_callback(message)
        current_failed = sorted(failed_indices)
        failed_indices = set()
        for idx in current_failed:
            if pause_event.is_set():
                raise DownloadPausedError("Pause requested by user.")
            segment_url = segments[idx - 1]
            part_path = os.path.join(parts_dir, "{0:05d}.ts".format(idx))
            try:
                segment_response = fetch(segment_url)
                with open(part_path, "wb") as segment_file:
                    segment_file.write(segment_response.content)
                downloaded_bytes += len(segment_response.content)
                session_downloaded_bytes += len(segment_response.content)
                if idx not in completed_segments:
                    completed_segments.add(idx)
                    mark_segment_complete(
                        manifest_path=manifest_path,
                        manifest_data=manifest_data,
                        manifest_lock=manifest_lock,
                        episode_number=episode_number,
                        segment_index=idx,
                    )
            except requests.RequestException:
                failed_indices.add(idx)
            finally:
                progress_renderer.update(
                    episode_name=episode_name,
                    line=format_progress_line(
                        min(len(completed_segments), len(segments)),
                        len(segments),
                        downloaded_bytes,
                        time.time() - started_at,
                        session_downloaded_bytes,
                    ),
                )

    if failed_indices:
        raise requests.RequestException(
            "Failed segments after retries: {0}".format(", ".join(str(i) for i in sorted(failed_indices)))
        )

    with open(output_path, "wb") as output_file:
        for idx in range(1, len(segments) + 1):
            part_path = os.path.join(parts_dir, "{0:05d}.ts".format(idx))
            if not os.path.exists(part_path):
                raise requests.RequestException("Missing segment part {0} for episode assembly.".format(idx))
            with open(part_path, "rb") as segment_file:
                output_file.write(segment_file.read())

    try:
        shutil.rmtree(parts_dir)
    except OSError:
        pass

    progress_renderer.finish_inline()
    return output_path


def download_single_episode(
    episode_number: int,
    anime_title: str,
    episode_url: str,
    output_dir: str,
    preferred_subtitle_mode: str,
    subtitle_output_mode: str,
    output_container_policy: OutputContainerPolicy,
    preferred_quality_label: str | None,
    progress_renderer: ProgressRenderer,
    update_episode_status: Callable[[int, str], None],
    verbose: bool,
    pause_event: threading.Event,
    manifest_path: str,
    manifest_data: dict,
    manifest_lock: threading.Lock,
    provider_order: list[str],
    episode_refs_by_provider: dict[str, str],
    diagnostics_callback: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    selected_episode_name = "{0} Episode {1}".format(anime_title, episode_number)
    progress_renderer.reserve_slot(selected_episode_name)
    update_episode_status(episode_number, "running")
    set_manifest_episode_status(
        manifest_path=manifest_path,
        manifest_data=manifest_data,
        manifest_lock=manifest_lock,
        episode_number=episode_number,
        status="running",
    )
    try:
        if verbose:
            locked_print("\nResolving stream for {0}".format(episode_url))
        prepared_stream = prepare_stream_with_fallback(
            episode_refs_by_provider=episode_refs_by_provider,
            provider_order=provider_order,
            preferred_subtitle_mode=preferred_subtitle_mode,
            preferred_quality_label=preferred_quality_label,
            prompt_for_subtitle_mode=False,
            prompt_for_quality=False,
            emit_logs=verbose,
            emit_diagnostics=verbose or diagnostics_callback is not None,
            diagnostics_callback=diagnostics_callback,
        )
        if (
            diagnostics_callback is not None
            and provider_order
            and prepared_stream.provider_key != provider_order[0]
        ):
            diagnostics_callback(
                "Episode {0}: using fallback provider '{1}'.".format(episode_number, prepared_stream.provider_key)
            )
        if preferred_subtitle_mode and prepared_stream.subtitle_mode != preferred_subtitle_mode:
            locked_print(
                "Episode {0}: requested mode '{1}' is unavailable for the resolved stream; using '{2}'.".format(
                    episode_number,
                    preferred_subtitle_mode,
                    prepared_stream.subtitle_mode,
                )
            )
        if verbose:
            locked_print(
                "Resolved playlist from provider '{0}': {1}".format(
                    prepared_stream.provider_key, prepared_stream.media_playlist_url
                )
            )
            if prepared_stream.subtitle_url:
                locked_print("Subtitle source found and will be downloaded as .vtt sidecar.")

        if prepared_stream.quality_label and verbose:
            locked_print("Using quality: {0}".format(prepared_stream.quality_label))

        downloaded_ts_file = download_hls_episode(
            episode_number=episode_number,
            episode_name=selected_episode_name,
            media_playlist_url=prepared_stream.media_playlist_url,
            output_dir=output_dir,
            progress_renderer=progress_renderer,
            verbose=verbose,
            pause_event=pause_event,
            manifest_path=manifest_path,
            manifest_data=manifest_data,
            manifest_lock=manifest_lock,
            diagnostics_callback=diagnostics_callback,
        )
        final_file = convert_ts_to_video(
            downloaded_ts_file,
            container_policy=output_container_policy,
            verbose=verbose,
        )
        if prepared_stream.subtitle_url:
            subtitle_file = download_subtitle_file(prepared_stream.subtitle_url, final_file)
            if subtitle_output_mode == "mux":
                final_file = mux_subtitle_into_video(final_file, subtitle_file, verbose=verbose)
            elif verbose:
                locked_print("Subtitle saved: {0}".format(subtitle_file))

        if verbose:
            locked_print("Download complete: {0}".format(final_file))
        set_manifest_episode_status(
            manifest_path=manifest_path,
            manifest_data=manifest_data,
            manifest_lock=manifest_lock,
            episode_number=episode_number,
            status="done",
            final_file=final_file,
        )
        return episode_number, final_file
    except DownloadPausedError as exc:
        set_manifest_episode_status(
            manifest_path=manifest_path,
            manifest_data=manifest_data,
            manifest_lock=manifest_lock,
            episode_number=episode_number,
            status="paused",
            error_message=str(exc),
        )
        raise
    except (NoSearchResultsError, ProviderError, requests.RequestException, OSError, ValueError) as exc:
        set_manifest_episode_status(
            manifest_path=manifest_path,
            manifest_data=manifest_data,
            manifest_lock=manifest_lock,
            episode_number=episode_number,
            status="failed",
            error_message=str(exc),
        )
        raise
    finally:
        progress_renderer.release_slot(selected_episode_name)
