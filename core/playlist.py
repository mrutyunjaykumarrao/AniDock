import re
from collections.abc import Iterable
from typing import Callable
from urllib.parse import urljoin

from core.models import PlaylistVariant
from core.network import fetch, locked_print


def read_non_comment_lines(m3u8_text: str) -> list[str]:
    return [line.strip() for line in m3u8_text.splitlines() if line.strip() and not line.startswith("#")]


def parse_ext_x_stream_inf_attributes(stream_inf_line: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for part in stream_inf_line.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        attributes[key.strip()] = value.strip().strip('"')
    return attributes


def extract_playlist_variants(master_playlist_url: str) -> list[PlaylistVariant]:
    playlist_response = fetch(master_playlist_url)
    playlist_text = playlist_response.text
    stream_matches = re.findall(r"#EXT-X-STREAM-INF:([^\n]+)\n([^\n]+)", playlist_text)
    variants: list[PlaylistVariant] = []

    for attributes_line, variant_path in stream_matches:
        attrs = parse_ext_x_stream_inf_attributes(attributes_line)
        bandwidth = int(attrs.get("BANDWIDTH", "0"))
        resolution = attrs.get("RESOLUTION", "")
        if "x" in resolution:
            quality_label = resolution.split("x", 1)[1] + "p"
        else:
            quality_label = attrs.get("NAME", "Auto")
        variants.append(
            PlaylistVariant(
                quality_label=quality_label,
                bandwidth=bandwidth,
                playlist_url=urljoin(master_playlist_url, variant_path.strip()),
            )
        )

    return variants


def select_quality_playlist(
    master_or_media_playlist_url: str,
    preferred_quality_label: str | None = None,
    prompt_for_quality: bool = True,
    emit_logs: bool = True,
    chooser: Callable[[int, str], int] | None = None,
) -> tuple[str, str | None]:
    variants = extract_playlist_variants(master_or_media_playlist_url)
    if not variants:
        if prompt_for_quality and emit_logs:
            locked_print("No quality variants found. Using direct playlist.")
        return master_or_media_playlist_url, None

    variants.sort(key=lambda variant: variant.bandwidth)
    if preferred_quality_label:
        for variant in variants:
            if variant.quality_label == preferred_quality_label:
                return variant.playlist_url, variant.quality_label
        fallback_variant = variants[-1]
        if emit_logs:
            locked_print(
                "Preferred quality '{0}' is unavailable for this episode. Falling back to '{1}'.".format(
                    preferred_quality_label, fallback_variant.quality_label
                )
            )
        return fallback_variant.playlist_url, fallback_variant.quality_label

    if not prompt_for_quality or chooser is None:
        fallback_variant = variants[-1]
        return fallback_variant.playlist_url, fallback_variant.quality_label

    print("\nAvailable quality options:")
    for index, variant in enumerate(variants, start=1):
        print(
            "[{0}] {1} ({2} kbps)".format(index, variant.quality_label, max(1, variant.bandwidth // 1000))
        )
    quality_choice = chooser(len(variants), "Select quality (1-{0}): ".format(len(variants)))
    selected_variant = variants[quality_choice - 1]
    print("Selected quality: {0}".format(selected_variant.quality_label))
    return selected_variant.playlist_url, selected_variant.quality_label


def iter_segments(media_playlist_url: str) -> Iterable[str]:
    media_playlist_response = fetch(media_playlist_url)
    for segment_line in read_non_comment_lines(media_playlist_response.text):
        yield urljoin(media_playlist_url, segment_line)

