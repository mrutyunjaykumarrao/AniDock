import os
import shutil
from collections import Counter
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin

import requests

from core.exceptions import NoSearchResultsError, ProviderError
from core.network import REQUEST_TIMEOUT_SECONDS, fetch, get_session, locked_print
from core.playlist import extract_playlist_variants
from core.models import OutputContainerPolicy
from core.site import prepare_stream_with_fallback

DEFAULT_DURATION_HEURISTIC_BPS = 750 * 1024
DEFAULT_SEGMENT_HEURISTIC_BYTES = 2_500_000
DEFAULT_EPISODE_HEURISTIC_BYTES = 650 * 1024 * 1024
BYTERANGE_SAFETY_MULTIPLIER = 1.03
SAMPLED_SEGMENT_SAFETY_MULTIPLIER = 1.10
BANDWIDTH_SAFETY_MULTIPLIER = 1.15


@dataclass(frozen=True)
class EpisodeSpaceEstimate:
    episode_number: int
    estimated_bytes: int
    source: str
    note: str


def format_bytes(value: int) -> str:
    size = float(max(0, value))
    units = ["B", "KB", "MB", "GB", "TB"]
    unit_index = 0
    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1
    return "{0:.2f} {1}".format(size, units[unit_index])


def parse_media_playlist(media_playlist_url: str) -> tuple[float, int, list[str]]:
    response = fetch(media_playlist_url)
    total_duration_seconds = 0.0
    byterange_total = 0
    segment_urls: list[str] = []

    for raw_line in response.text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            duration_token = line.split(":", 1)[1].split(",", 1)[0].strip()
            try:
                total_duration_seconds += float(duration_token)
            except ValueError:
                pass
            continue
        if line.startswith("#EXT-X-BYTERANGE:"):
            byterange_token = line.split(":", 1)[1].split("@", 1)[0].strip()
            try:
                byterange_total += int(byterange_token)
            except ValueError:
                pass
            continue
        if line.startswith("#"):
            continue
        segment_urls.append(urljoin(media_playlist_url, line))

    return total_duration_seconds, byterange_total, segment_urls


def _sample_segment_sizes(segment_urls: list[str]) -> list[int]:
    if not segment_urls:
        return []

    sample_indices = sorted({0, len(segment_urls) // 2, len(segment_urls) - 1})
    lengths: list[int] = []
    session = get_session()

    for index in sample_indices:
        segment_url = segment_urls[index]
        try:
            response = session.head(segment_url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
            response.raise_for_status()
            content_length = response.headers.get("Content-Length", "").strip()
            if content_length.isdigit():
                parsed = int(content_length)
                if parsed > 0:
                    lengths.append(parsed)
        except requests.RequestException:
            continue

    return lengths


def _select_variant_bandwidth(
    master_or_media_playlist_url: str,
    selected_media_playlist_url: str,
    selected_quality_label: str | None,
) -> int | None:
    variants = extract_playlist_variants(master_or_media_playlist_url)
    if not variants:
        return None

    if selected_quality_label:
        for variant in variants:
            if variant.quality_label == selected_quality_label:
                return variant.bandwidth

    for variant in variants:
        if variant.playlist_url == selected_media_playlist_url:
            return variant.bandwidth

    variants.sort(key=lambda item: item.bandwidth)
    return variants[-1].bandwidth


def estimate_episode_space(
    episode_number: int,
    episode_refs_by_provider: dict[str, str],
    provider_order: list[str],
    preferred_subtitle_mode: str,
    preferred_quality_label: str | None,
    emit_diagnostics: bool,
) -> EpisodeSpaceEstimate:
    try:
        prepared_stream = prepare_stream_with_fallback(
            episode_refs_by_provider=episode_refs_by_provider,
            provider_order=provider_order,
            preferred_subtitle_mode=preferred_subtitle_mode,
            preferred_quality_label=preferred_quality_label,
            prompt_for_subtitle_mode=False,
            prompt_for_quality=False,
            emit_logs=False,
            emit_diagnostics=emit_diagnostics,
        )
        media_playlist_url = prepared_stream.media_playlist_url
        selected_quality_label = prepared_stream.quality_label

        duration_seconds, byterange_total, segment_urls = parse_media_playlist(media_playlist_url)

        if byterange_total > 0:
            estimated_bytes = max(
                int(byterange_total * BYTERANGE_SAFETY_MULTIPLIER),
                80 * 1024 * 1024,
            )
            return EpisodeSpaceEstimate(
                episode_number=episode_number,
                estimated_bytes=estimated_bytes,
                source="playlist byte-range metadata",
                note="Episode {0}: estimated from EXT-X-BYTERANGE metadata.".format(episode_number),
            )

        sampled_lengths = _sample_segment_sizes(segment_urls)
        if sampled_lengths:
            average_segment_size = sum(sampled_lengths) / len(sampled_lengths)
            estimated_bytes = max(
                int(average_segment_size * len(segment_urls) * SAMPLED_SEGMENT_SAFETY_MULTIPLIER),
                80 * 1024 * 1024,
            )
            return EpisodeSpaceEstimate(
                episode_number=episode_number,
                estimated_bytes=estimated_bytes,
                source="sampled segment headers",
                note=(
                    "Episode {0}: estimated from Content-Length of sampled segments "
                    "({1}/{2} sampled)."
                ).format(episode_number, len(sampled_lengths), max(1, len({0, len(segment_urls) // 2, len(segment_urls) - 1}))),
            )

        selected_bandwidth = _select_variant_bandwidth(
            media_playlist_url,
            media_playlist_url,
            selected_quality_label,
        )
        if duration_seconds > 0 and selected_bandwidth:
            estimated_bytes = max(
                int((selected_bandwidth * duration_seconds / 8.0) * BANDWIDTH_SAFETY_MULTIPLIER),
                80 * 1024 * 1024,
            )
            return EpisodeSpaceEstimate(
                episode_number=episode_number,
                estimated_bytes=estimated_bytes,
                source="variant bandwidth + duration",
                note=(
                    "Episode {0}: estimated from stream BANDWIDTH ({1} bps) "
                    "and playlist duration ({2:.0f}s)."
                ).format(episode_number, selected_bandwidth, duration_seconds),
            )

        if duration_seconds > 0:
            estimated_bytes = max(int(duration_seconds * DEFAULT_DURATION_HEURISTIC_BPS), 80 * 1024 * 1024)
            return EpisodeSpaceEstimate(
                episode_number=episode_number,
                estimated_bytes=estimated_bytes,
                source="duration heuristic",
                note=(
                    "Episode {0}: fallback estimate using duration-only heuristic "
                    "({1:.0f}s at conservative 6 Mbps)."
                ).format(episode_number, duration_seconds),
            )

        if segment_urls:
            estimated_bytes = max(len(segment_urls) * DEFAULT_SEGMENT_HEURISTIC_BYTES, 80 * 1024 * 1024)
            return EpisodeSpaceEstimate(
                episode_number=episode_number,
                estimated_bytes=estimated_bytes,
                source="segment-count heuristic",
                note=(
                    "Episode {0}: fallback estimate using segment count heuristic "
                    "({1} segments at ~2.5 MB/segment)."
                ).format(episode_number, len(segment_urls)),
            )

        return EpisodeSpaceEstimate(
            episode_number=episode_number,
            estimated_bytes=DEFAULT_EPISODE_HEURISTIC_BYTES,
            source="fixed heuristic",
            note="Episode {0}: no usable playlist metadata; using conservative fixed fallback.".format(episode_number),
        )
    except (NoSearchResultsError, ProviderError, requests.RequestException, OSError, ValueError) as exc:
        return EpisodeSpaceEstimate(
            episode_number=episode_number,
            estimated_bytes=DEFAULT_EPISODE_HEURISTIC_BYTES,
            source="fixed heuristic",
            note="Episode {0}: metadata unavailable ({1}); using conservative fixed fallback.".format(episode_number, exc),
        )


def run_disk_space_preflight(
    episode_numbers: Iterable[int],
    episode_provider_url_map: dict[int, dict[str, str]],
    provider_order: list[str],
    output_dir: str,
    preferred_subtitle_mode: str,
    preferred_quality_label: str | None,
    subtitle_output_mode: str,
    output_container_policy: OutputContainerPolicy,
    worker_count: int,
    emit_logs: bool = True,
) -> bool:
    def log(message: str) -> None:
        if emit_logs:
            locked_print(message)

    queued = list(episode_numbers)
    if not queued:
        log("Disk preflight skipped: no queued episodes.")
        return True

    log("\nRunning disk space preflight...")
    estimates = [
        estimate_episode_space(
            episode_number=episode_number,
            episode_refs_by_provider=episode_provider_url_map.get(episode_number, {}),
            provider_order=provider_order,
            preferred_subtitle_mode=preferred_subtitle_mode,
            preferred_quality_label=preferred_quality_label,
            emit_diagnostics=emit_logs,
        )
        for episode_number in queued
    ]

    total_estimated_bytes = sum(item.estimated_bytes for item in estimates)
    max_episode_bytes = max(item.estimated_bytes for item in estimates)

    ffmpeg_available = shutil.which("ffmpeg") is not None
    does_remux = output_container_policy != "ts" and ffmpeg_available
    remux_multiplier = 1.10 if does_remux else 0.0
    subtitle_mux_multiplier = 1.0 if subtitle_output_mode == "mux" and ffmpeg_available else 0.0
    transient_multiplier = 1.0 + remux_multiplier + subtitle_mux_multiplier
    transient_buffer_bytes = int(max_episode_bytes * max(1, worker_count) * transient_multiplier)

    minimum_required_bytes = total_estimated_bytes
    recommended_required_bytes = total_estimated_bytes + transient_buffer_bytes
    free_bytes = shutil.disk_usage(output_dir).free

    source_counts = Counter(item.source for item in estimates)
    log(
        "Disk preflight: episodes={0}, estimate={1}, free={2}".format(
            len(queued),
            format_bytes(total_estimated_bytes),
            format_bytes(free_bytes),
        )
    )
    log("Estimate source breakdown: {0}".format(", ".join(
        "{0}: {1}".format(source, count) for source, count in sorted(source_counts.items())
    )))
    log(
        "Minimum required (final files): {0}".format(format_bytes(minimum_required_bytes))
    )
    log(
        "Recommended required (includes temp workspace for parts/remux/mux): {0}".format(
            format_bytes(recommended_required_bytes)
        )
    )

    low_confidence_estimates = [
        item for item in estimates if "heuristic" in item.source
    ]
    if low_confidence_estimates:
        preview = "; ".join(item.note for item in low_confidence_estimates[:3])
        if len(low_confidence_estimates) > 3:
            preview += "; ..."
        log(
            "Preflight note: conservative heuristic fallback used for {0} episode(s). {1}".format(
                len(low_confidence_estimates), preview
            )
        )

    if free_bytes < minimum_required_bytes:
        missing_bytes = minimum_required_bytes - free_bytes
        log(
            "Disk preflight failed: clearly insufficient free space. Need at least {0} more before starting.".format(
                format_bytes(missing_bytes)
            )
        )
        return False

    if free_bytes < recommended_required_bytes:
        log(
            "Disk preflight warning: final output may fit, but temporary peak usage for parts/remux/mux may exceed free space."
        )

    log("Disk preflight passed.")
    return True
