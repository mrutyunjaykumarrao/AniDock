import json
import os
import re
from urllib.parse import parse_qs, urlparse


def sanitize_file_part(value: str) -> str:
    return re.sub(r"[\\\\/:*?\"<>|]+", "_", value).strip()


def load_queue_selection_from_file(file_path: str) -> str:
    absolute_path = os.path.abspath(os.path.expanduser(file_path.strip()))
    if not os.path.isfile(absolute_path):
        raise ValueError("Queue file not found: {0}".format(absolute_path))

    lower_path = absolute_path.lower()
    if lower_path.endswith(".json"):
        with open(absolute_path, "r", encoding="utf-8") as queue_file:
            payload = json.load(queue_file)

        items: list[str | int]
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict) and "episodes" in payload and isinstance(payload["episodes"], list):
            items = payload["episodes"]
        else:
            raise ValueError("JSON queue must be a list or an object with an 'episodes' list.")

        normalized_items: list[str] = []
        for item in items:
            if isinstance(item, int):
                normalized_items.append(str(item))
                continue
            if isinstance(item, str) and item.strip():
                normalized_items.append(item.strip())
                continue
            raise ValueError("Queue JSON contains unsupported entry: {0}".format(item))
        return ",".join(normalized_items)

    with open(absolute_path, "r", encoding="utf-8") as queue_file:
        raw_text = queue_file.read()
    normalized = raw_text.replace("\n", ",").replace("\t", ",").replace(" ", "")
    if not normalized.strip(","):
        raise ValueError("Queue file is empty: {0}".format(absolute_path))
    return normalized


def episode_sort_key(url: str) -> tuple[int, float]:
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    if "ep" in query_params and query_params["ep"]:
        token = query_params["ep"][0]
        if token.replace(".", "", 1).isdigit():
            return (0, float(token))

    watch_match = re.search(r"/ep-(\d+(?:\.\d+)?)$", parsed.path)
    if watch_match:
        return (0, float(watch_match.group(1)))

    match = re.search(r"-episode-(\d+(?:\.\d+)?)", url)
    if match:
        return (0, float(match.group(1)))
    return (1, float("inf"))


def extract_episode_number_from_url(url: str) -> int | None:
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    if "ep" in query_params and query_params["ep"]:
        token = query_params["ep"][0]
        if token.isdigit():
            return int(token)

    watch_match = re.search(r"/ep-(\d+)$", parsed.path)
    if watch_match:
        return int(watch_match.group(1))

    match = re.search(r"-episode-(\d+)$", url)
    if match:
        return int(match.group(1))
    return None


def parse_episode_selection(raw_selection: str, available_episode_numbers: set[int]) -> list[int]:
    selection = raw_selection.strip()
    if not selection:
        raise ValueError("Please enter an episode number or a range.")

    requested_episodes: list[int] = []
    seen: set[int] = set()
    parts = [part.strip() for part in selection.split(",") if part.strip()]
    if not parts:
        raise ValueError("Please enter a valid episode number, range, or comma-separated list.")

    for part in parts:
        if "-" not in part:
            if not part.isdigit():
                raise ValueError("Please enter valid episode numbers (examples: 12, 1-12, 1,7,20).")
            episode_number = int(part)
            if episode_number not in available_episode_numbers:
                raise ValueError("Episode {0} is not available.".format(episode_number))
            if episode_number not in seen:
                seen.add(episode_number)
                requested_episodes.append(episode_number)
            continue

        start_raw, end_raw = [value.strip() for value in part.split("-", 1)]
        if not start_raw.isdigit() or not end_raw.isdigit():
            raise ValueError("Range must be in the format start-end (for example 1-12).")

        start_episode = int(start_raw)
        end_episode = int(end_raw)
        if start_episode > end_episode:
            raise ValueError("Range start must be less than or equal to range end.")

        for episode_number in range(start_episode, end_episode + 1):
            if episode_number not in available_episode_numbers:
                raise ValueError("Episode {0} is not available.".format(episode_number))
            if episode_number not in seen:
                seen.add(episode_number)
                requested_episodes.append(episode_number)

    if not requested_episodes:
        raise ValueError("No valid episodes were selected.")

    return requested_episodes
