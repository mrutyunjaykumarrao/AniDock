import os
import shutil
import subprocess
import json

from core.models import OutputContainerPolicy
from core.network import fetch, locked_print


def has_enough_space_for_conversion(source_path: str, required_multiplier: float = 1.10) -> bool:
    source_size = os.path.getsize(source_path)
    target_dir = os.path.dirname(source_path) or "."
    free_space = shutil.disk_usage(target_dir).free
    required_space = int(source_size * required_multiplier)
    return free_space >= required_space


def file_has_video_stream(file_path: str) -> bool:
    ffprobe_path = shutil.which("ffprobe")
    if not ffprobe_path:
        return os.path.exists(file_path) and os.path.getsize(file_path) > 0
    result = subprocess.run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            file_path,
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _ffprobe_media_info(file_path: str) -> dict | None:
    ffprobe_path = shutil.which("ffprobe")
    if not ffprobe_path:
        return None
    result = subprocess.run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type:format=duration",
            "-of",
            "json",
            file_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        parsed = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _media_stream_counts(media_info: dict | None) -> tuple[int, int]:
    if not media_info:
        return 0, 0
    streams = media_info.get("streams")
    if not isinstance(streams, list):
        return 0, 0
    video_streams = 0
    audio_streams = 0
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec_type = stream.get("codec_type")
        if codec_type == "video":
            video_streams += 1
        elif codec_type == "audio":
            audio_streams += 1
    return video_streams, audio_streams


def _media_duration_seconds(media_info: dict | None) -> float | None:
    if not media_info:
        return None
    format_info = media_info.get("format")
    if not isinstance(format_info, dict):
        return None
    duration_value = format_info.get("duration")
    try:
        duration = float(duration_value)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    return duration


def verify_remux_output(source_path: str, output_path: str) -> tuple[bool, str]:
    ffprobe_available = shutil.which("ffprobe") is not None
    if not ffprobe_available:
        if file_has_video_stream(output_path):
            return True, ""
        return False, "output file has no valid video stream"

    source_info = _ffprobe_media_info(source_path)
    output_info = _ffprobe_media_info(output_path)
    if output_info is None:
        return False, "ffprobe could not inspect converted output"

    _, source_audio_streams = _media_stream_counts(source_info)
    output_video_streams, output_audio_streams = _media_stream_counts(output_info)
    if output_video_streams < 1:
        return False, "output file has no valid video stream"
    if source_audio_streams > 0 and output_audio_streams < 1:
        return False, "output file missing expected audio stream"

    output_duration = _media_duration_seconds(output_info)
    if output_duration is not None and output_duration < 1.0:
        return False, "output duration too short to be valid"

    source_duration = _media_duration_seconds(source_info)
    if source_duration is not None and output_duration is not None:
        duration_delta = abs(source_duration - output_duration)
        allowed_delta = max(2.0, source_duration * 0.10)
        if duration_delta > allowed_delta:
            return False, "output duration inconsistent with source stream"

    return True, ""


def run_remux(ffmpeg_path: str, ts_path: str, output_path: str, container: str) -> tuple[bool, str]:
    temp_output = output_path + ".tmp." + container
    command = [
        ffmpeg_path,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "mpegts",
        "-i",
        ts_path,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
    ]
    if container == "mp4":
        command.extend(["-movflags", "+faststart"])
    command.append(temp_output)

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        try:
            os.remove(temp_output)
        except OSError:
            pass
        return False, result.stderr.strip() or "unknown ffmpeg error"

    verified, verification_error = verify_remux_output(ts_path, temp_output)
    if not verified:
        try:
            os.remove(temp_output)
        except OSError:
            pass
        return False, verification_error

    os.replace(temp_output, output_path)
    return True, ""


def convert_ts_to_video(
    ts_path: str,
    container_policy: OutputContainerPolicy = "auto",
    verbose: bool = True,
) -> str:
    if container_policy == "ts":
        if verbose:
            locked_print("Container policy is ts. Keeping .ts file as output.")
        return ts_path

    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        if verbose:
            locked_print("ffmpeg not found. Keeping .ts file as output.")
        return ts_path

    if not has_enough_space_for_conversion(ts_path):
        if verbose:
            locked_print("Not enough free disk space for conversion. Keeping .ts file.")
        return ts_path

    if container_policy in ("mp4", "mkv"):
        target_path = os.path.splitext(ts_path)[0] + "." + container_policy
        success, error_message = run_remux(ffmpeg_path, ts_path, target_path, container_policy)
        if success:
            try:
                os.remove(ts_path)
            except OSError:
                pass
            if verbose:
                locked_print("Converted to {0}: {1}".format(container_policy.upper(), target_path))
            return target_path
        if verbose:
            locked_print(
                "{0} conversion failed: {1}".format(container_policy.upper(), error_message)
            )
            locked_print("Keeping .ts file: {0}".format(ts_path))
        return ts_path

    mp4_path = os.path.splitext(ts_path)[0] + ".mp4"
    success, error_message = run_remux(ffmpeg_path, ts_path, mp4_path, "mp4")
    if success:
        try:
            os.remove(ts_path)
        except OSError:
            pass
        if verbose:
            locked_print("Converted to MP4: {0}".format(mp4_path))
        return mp4_path

    if verbose:
        locked_print("MP4 conversion failed: {0}".format(error_message))
    mkv_path = os.path.splitext(ts_path)[0] + ".mkv"
    success, error_message = run_remux(ffmpeg_path, ts_path, mkv_path, "mkv")
    if success:
        try:
            os.remove(ts_path)
        except OSError:
            pass
        if verbose:
            locked_print("Converted to MKV: {0}".format(mkv_path))
        return mkv_path

    if verbose:
        locked_print("MKV conversion failed: {0}".format(error_message))
        locked_print("Keeping .ts file: {0}".format(ts_path))
    return ts_path


def download_subtitle_file(subtitle_url: str, media_file_path: str) -> str:
    subtitle_response = fetch(subtitle_url)
    subtitle_path = os.path.splitext(media_file_path)[0] + ".vtt"
    with open(subtitle_path, "wb") as subtitle_file:
        subtitle_file.write(subtitle_response.content)
    return subtitle_path


def mux_subtitle_into_video(video_file_path: str, subtitle_file_path: str, verbose: bool = True) -> str:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        if verbose:
            locked_print("ffmpeg not found. Keeping subtitle as sidecar file.")
        return video_file_path

    extension = os.path.splitext(video_file_path)[1].lower()
    temp_output = video_file_path + ".mux.tmp" + extension
    command = [
        ffmpeg_path,
        "-y",
        "-loglevel",
        "error",
        "-i",
        video_file_path,
        "-i",
        subtitle_file_path,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-map",
        "1:0",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-metadata:s:s:0",
        "language=eng",
    ]
    if extension == ".mp4":
        command.extend(["-c:s", "mov_text", "-movflags", "+faststart"])
    else:
        command.extend(["-c:s", "copy"])
    command.append(temp_output)

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        try:
            os.remove(temp_output)
        except OSError:
            pass
        if verbose:
            locked_print(
                "Subtitle mux failed; keeping sidecar. Reason: {0}".format(
                    result.stderr.strip() or "unknown ffmpeg error"
                )
            )
        return video_file_path

    if not file_has_video_stream(temp_output):
        try:
            os.remove(temp_output)
        except OSError:
            pass
        if verbose:
            locked_print("Subtitle mux output invalid; keeping sidecar file.")
        return video_file_path

    os.replace(temp_output, video_file_path)
    try:
        os.remove(subtitle_file_path)
    except OSError:
        pass
    if verbose:
        locked_print("Embedded subtitle track into: {0}".format(video_file_path))
    return video_file_path
