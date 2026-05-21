import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from core.exceptions import (
    InvalidAnimeNameError,
    NoSearchResultsError,
    ProviderCapabilityError,
    ProviderError,
    ProviderParsingError,
    ProviderUnavailableError,
)
from core.input_parsing import episode_sort_key
from core.models import SearchResult
from providers.base import AnimeReference, ProviderAdapter, ProviderCapabilities, ResolvedStream, StreamPreferences

REQUEST_TIMEOUT_SECONDS = 25
MAX_FETCH_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.2
MIRROR_BASE_URLS = (
    "https://hianime.re",
)
SHUTDOWN_MESSAGE = "It's time to say goodbye"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class _ServerCandidate:
    server_id: str
    label: str
    subtitle_mode: str


class HiAnimeProvider(ProviderAdapter):
    key = "hianime"
    name = "HiAnime"
    capabilities = ProviderCapabilities(
        subtitle_modes=("SUB", "DUB"),
        quality_selection="HLS variants from source payload; defaults to highest available.",
        notes=(
            "HiAnime source API can change frequently. "
            "Adapter raises explicit provider errors when encrypted or unsupported payloads are returned."
        ),
        supports_subtitle_preference=True,
    )

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._episode_meta_cache: dict[str, dict[int, dict[str, Any]]] = {}

    def search(self, query: str) -> list[SearchResult]:
        self._guard_capability("supports_search", "Search is not supported by this provider.")
        if not query or not query.strip():
            raise InvalidAnimeNameError("Anime name can't be empty.")

        errors: list[str] = []
        parsed_without_results = False
        for base_url in MIRROR_BASE_URLS:
            try:
                query_token = quote_plus(query)
                results: list[SearchResult] = []
                for path in ("/search?keyword={0}".format(query_token), "/filter?keyword={0}".format(query_token)):
                    search_page = self._fetch(base_url, path)
                    search_soup = BeautifulSoup(search_page.text, "lxml")
                    extracted = self._extract_search_results(search_soup, base_url)
                    if extracted:
                        results = extracted
                        break
                    parsed_without_results = True
                if results:
                    return results
            except (requests.RequestException, ProviderParsingError, ProviderUnavailableError) as exc:
                errors.append("{0}: {1}".format(base_url, exc))

        if parsed_without_results:
            raise NoSearchResultsError("No anime found for the provided query on HiAnime.")
        raise ProviderUnavailableError("HiAnime search failed on all known mirrors: {0}".format("; ".join(errors)))

    def list_episodes(self, anime_ref: AnimeReference | str) -> list[str]:
        self._guard_capability("supports_episode_listing", "Episode listing is not supported by this provider.")
        anime_url = anime_ref if isinstance(anime_ref, str) else anime_ref.category_url
        base_url = self._pick_base_url(anime_url)

        anime_page = self._fetch(base_url, anime_url)
        anime_id = self._extract_anime_id(anime_page.text)
        endpoint = "/ajax/episode/list/{0}".format(anime_id)
        episode_payload = self._fetch_json(
            base_url,
            endpoint,
            headers={
                "Referer": anime_url,
                "X-Requested-With": "XMLHttpRequest",
            },
        )

        html_fragment = str(episode_payload.get("html", "")).strip()
        if not html_fragment:
            html_fragment = str(episode_payload.get("result", "")).strip()
        if not html_fragment:
            raise ProviderParsingError("HiAnime episode API did not return a usable HTML payload.")

        episodes_soup = BeautifulSoup(html_fragment, "lxml")
        episode_links: list[str] = []
        seen: set[str] = set()
        anime_slug = anime_url.rstrip("/").split("/anime/")[-1]
        watch_base = urljoin(base_url + "/", "watch/{0}".format(anime_slug))
        episode_meta_map: dict[int, dict[str, Any]] = {}
        for anchor in episodes_soup.select("a.ep-item, a.ssl-item.ep-item, a[href*='/watch/']"):
            data_num = str(anchor.get("data-num", "")).strip()
            data_slug = str(anchor.get("data-slug", "")).strip()
            episode_number: int | None = None
            if data_num.isdigit():
                episode_number = int(data_num)
            elif data_slug.isdigit():
                episode_number = int(data_slug)
            absolute = ""
            if data_slug:
                absolute = "{0}/ep-{1}".format(watch_base, data_slug)
            elif data_num:
                absolute = "{0}/ep-{1}".format(watch_base, data_num)
            if not absolute:
                href = str(anchor.get("href", "")).strip()
                if not href or href == "#":
                    continue
                absolute = urljoin(base_url, href)
            if "/watch/" not in absolute:
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            episode_links.append(absolute)
            if episode_number is not None:
                episode_meta_map[episode_number] = {
                    "has_sub": str(anchor.get("data-sub", "")).strip() == "1",
                    "has_dub": str(anchor.get("data-dub", "")).strip() == "1",
                    "data_ids": str(anchor.get("data-ids", "")).strip(),
                    "slug": data_slug or data_num,
                }

        if not episode_links:
            raise NoSearchResultsError("No episodes found for the selected HiAnime title.")

        if episode_meta_map:
            self._episode_meta_cache[watch_base.rstrip("/")] = episode_meta_map

        episode_links.sort(key=episode_sort_key)
        return episode_links

    def available_subtitle_modes(self, episode_ref: str) -> tuple[str, ...]:
        metadata = self._get_episode_metadata(episode_ref)
        modes: list[str] = []
        if metadata.get("has_sub"):
            modes.append("SUB")
        if metadata.get("has_dub"):
            modes.append("DUB")
        return tuple(modes) or ("SUB",)

    def resolve_stream(self, episode_ref: str, preferences: StreamPreferences | None = None) -> ResolvedStream:
        self._guard_capability("supports_stream_resolution", "Stream resolution is not supported by this provider.")
        selected_preferences = preferences or StreamPreferences()
        available_modes = self.available_subtitle_modes(episode_ref)
        if selected_preferences.subtitle_mode and selected_preferences.subtitle_mode not in available_modes:
            raise ProviderCapabilityError(
                "HiAnime episode supports subtitle mode(s): {0}".format(
                    ", ".join(available_modes)
                )
            )
        selected_mode = selected_preferences.subtitle_mode or ("SUB" if "SUB" in available_modes else available_modes[0])
        if (
            selected_preferences.subtitle_mode is None
            and selected_preferences.prompt_for_mode
            and selected_preferences.chooser
            and len(available_modes) > 1
        ):
            print("\nAudio/stream mode options:")
            labels = {
                "SUB": "SUB (Japanese audio + subtitles)",
                "DUB": "DUB (dubbed audio when available)",
            }
            for index, mode in enumerate(available_modes, start=1):
                print("[{0}] {1}".format(index, labels.get(mode, mode)))
            mode_choice = selected_preferences.chooser(len(available_modes), "Select mode (1-{0}): ".format(len(available_modes)))
            selected_mode = available_modes[mode_choice - 1]

        base_url = self._pick_base_url(episode_ref)
        episode_page = self._fetch(base_url, episode_ref)
        episode_id_candidates = self._extract_episode_id_candidates(episode_ref, episode_page.text)
        server_candidates: list[_ServerCandidate] = []
        server_errors: list[str] = []
        for episode_id in episode_id_candidates:
            try:
                servers_payload = self._fetch_json(base_url, "/ajax/v2/episode/servers?episodeId={0}".format(episode_id))
                extracted = self._extract_server_candidates(servers_payload)
                if extracted:
                    server_candidates = extracted
                    break
            except (ProviderUnavailableError, ProviderParsingError) as exc:
                server_errors.append(str(exc))
        if not server_candidates:
            raise ProviderUnavailableError(
                "HiAnime server API is unavailable for this episode. Attempts: {0}".format(
                    "; ".join(server_errors) if server_errors else "no server candidates returned"
                )
            )
        filtered_candidates = [candidate for candidate in server_candidates if candidate.subtitle_mode == selected_mode]
        if not filtered_candidates:
            filtered_candidates = server_candidates

        for server in filtered_candidates:
            sources_payload = self._fetch_json(base_url, "/ajax/v2/episode/sources?id={0}".format(server.server_id))
            if bool(sources_payload.get("encrypted")):
                raise ProviderCapabilityError(
                    "HiAnime returned encrypted stream sources for server '{0}'. Decryption is not implemented.".format(
                        server.label
                    )
                )
            playlist_url = self._extract_playlist_url(sources_payload)
            if not playlist_url:
                embed_url = str(sources_payload.get("link", "")).strip()
                if embed_url:
                    playlist_url = self._extract_m3u8_from_embed(embed_url)
            if playlist_url:
                return ResolvedStream(
                    playlist_url=playlist_url,
                    subtitle_url=self._extract_subtitle_url(sources_payload),
                    subtitle_mode=server.subtitle_mode,
                )

        raise ProviderParsingError("Failed to resolve a playable HLS playlist from HiAnime server sources.")

    def _guard_capability(self, field: str, message: str) -> None:
        if not getattr(self.capabilities, field):
            raise ProviderCapabilityError(message)

    def _pick_base_url(self, reference_url: str) -> str:
        parsed = urlparse(reference_url)
        if parsed.scheme and parsed.netloc:
            return "{0}://{1}".format(parsed.scheme, parsed.netloc)
        return MIRROR_BASE_URLS[0]

    def _fetch(self, base_url: str, path_or_url: str, headers: dict[str, str] | None = None) -> requests.Response:
        target_url = path_or_url if path_or_url.startswith("http") else urljoin(base_url, path_or_url)
        last_error: requests.RequestException | None = None
        for attempt in range(1, MAX_FETCH_RETRIES + 1):
            try:
                request_headers = {"Referer": base_url + "/"}
                if headers:
                    request_headers.update(headers)
                response = self._session.get(target_url, timeout=REQUEST_TIMEOUT_SECONDS, headers=request_headers)
                response.raise_for_status()
                if SHUTDOWN_MESSAGE in response.text:
                    raise ProviderUnavailableError(
                        "HiAnime mirror returned shutdown message: {0}".format(target_url)
                    )
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt == MAX_FETCH_RETRIES:
                    break
        raise ProviderUnavailableError(
            "Failed to fetch {0} after {1} attempts: {2}".format(target_url, MAX_FETCH_RETRIES, last_error)
        ) from last_error

    def _fetch_json(self, base_url: str, path_or_url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        response = self._fetch(base_url, path_or_url, headers=headers)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderParsingError("Expected JSON response from HiAnime but received invalid payload.") from exc
        if not isinstance(payload, dict):
            raise ProviderParsingError("Expected JSON object from HiAnime API endpoint.")
        return payload

    def _extract_search_results(self, search_soup: BeautifulSoup, base_url: str) -> list[SearchResult]:
        selectors = (
            "h3.film-name a[href*='/anime/']",
            "div.flw-item h3.film-name a.dynamic-name[href]",
            "a.film-name.dynamic-name[href]",
            "a.dynamic-name[href]",
            "div.film_list-wrap a[href*='/anime/']",
        )
        results: list[SearchResult] = []
        seen: set[str] = set()
        for selector in selectors:
            for anchor in search_soup.select(selector):
                href = str(anchor.get("href", "")).strip()
                if not href:
                    continue
                absolute = urljoin(base_url, href)
                if "/anime/" not in absolute:
                    continue
                if absolute in seen:
                    continue
                seen.add(absolute)
                title = anchor.get("title") or anchor.get_text(" ", strip=True)
                if not title:
                    continue
                results.append(SearchResult(title=str(title).strip(), category_url=absolute))
            if results:
                break
        return results

    def _extract_anime_id(self, html: str) -> str:
        id_patterns = (
            r'data-id=["\'](\d+)["\']',
            r'anime_id["\']?\s*[:=]\s*["\']?(\d+)["\']?',
        )
        for pattern in id_patterns:
            match = re.search(pattern, html)
            if match:
                return match.group(1)
        raise ProviderParsingError("Could not extract HiAnime anime ID from anime detail page.")

    def _extract_episode_id_candidates(self, episode_url: str, html: str) -> list[str]:
        candidates: list[str] = []
        parsed = urlparse(episode_url)
        params = parse_qs(parsed.query)
        if "eid" in params and params["eid"]:
            candidates.append(params["eid"][0])
        if "ep" in params and params["ep"]:
            ep_token = params["ep"][0]
            if ep_token.isdigit() and int(ep_token) >= 1000:
                candidates.append(ep_token)

        metadata = self._get_episode_metadata(episode_url)
        data_ids = str(metadata.get("data_ids", "")).strip()
        if data_ids:
            candidates.append(data_ids)

        id_patterns = (
            r'episode_id["\']?\s*[:=]\s*["\']?(\d+)["\']?',
            r'data-episode-id=["\'](\d+)["\']',
        )
        for pattern in id_patterns:
            match = re.search(pattern, html)
            if match:
                candidates.append(match.group(1))

        unique_candidates: list[str] = []
        for candidate in candidates:
            token = str(candidate).strip()
            if not token or token in unique_candidates:
                continue
            unique_candidates.append(token)
        if not unique_candidates:
            raise ProviderParsingError("Could not extract HiAnime episode ID from episode page.")
        return unique_candidates

    def _extract_episode_number_from_ref(self, episode_ref: str) -> int | None:
        parsed = urlparse(episode_ref)
        query_values = parse_qs(parsed.query)
        if "ep" in query_values and query_values["ep"] and query_values["ep"][0].isdigit():
            return int(query_values["ep"][0])
        match = re.search(r"/ep-(\d+)$", parsed.path)
        if match:
            return int(match.group(1))
        return None

    def _extract_watch_base(self, episode_ref: str) -> str | None:
        parsed = urlparse(episode_ref)
        match = re.search(r"(/watch/[^/?]+)", parsed.path)
        if not match:
            return None
        return "{0}://{1}{2}".format(parsed.scheme or "https", parsed.netloc or "hianime.re", match.group(1))

    def _get_episode_metadata(self, episode_ref: str) -> dict[str, Any]:
        episode_number = self._extract_episode_number_from_ref(episode_ref)
        watch_base = self._extract_watch_base(episode_ref)
        if episode_number is None or watch_base is None:
            return {}

        cached = self._episode_meta_cache.get(watch_base.rstrip("/"), {})
        if episode_number in cached:
            return cached[episode_number]

        parsed_watch = urlparse(watch_base)
        watch_slug = parsed_watch.path.split("/watch/")[-1]
        anime_url = "{0}://{1}/anime/{2}".format(parsed_watch.scheme or "https", parsed_watch.netloc or "hianime.re", watch_slug)
        try:
            self.list_episodes(anime_url)
        except (NoSearchResultsError, ProviderError, requests.RequestException, OSError, ValueError):
            return {}
        refreshed = self._episode_meta_cache.get(watch_base.rstrip("/"), {})
        return refreshed.get(episode_number, {})

    def _extract_server_candidates(self, servers_payload: dict[str, Any]) -> list[_ServerCandidate]:
        html_fragment = str(servers_payload.get("html", "")).strip()
        if not html_fragment:
            return []
        servers_soup = BeautifulSoup(html_fragment, "lxml")
        candidates: list[_ServerCandidate] = []
        seen: set[str] = set()
        for node in servers_soup.select("[data-id]"):
            server_id = str(node.get("data-id", "")).strip()
            if not server_id or server_id in seen:
                continue
            seen.add(server_id)
            label = " ".join(node.get_text(" ", strip=True).split()) or "Unknown"
            subtitle_mode = "SUB"
            class_hints = " ".join(str(value) for value in (node.get("class") or []))
            parent = node.find_parent(["div", "ul", "section"])
            parent_text = parent.get_text(" ", strip=True).upper() if parent else ""
            parent_classes = " ".join(str(value) for value in (parent.get("class", []) if parent else []))
            hint_blob = " ".join([class_hints, parent_classes, parent_text]).lower()
            if "dub" in hint_blob:
                subtitle_mode = "DUB"
            candidates.append(_ServerCandidate(server_id=server_id, label=label, subtitle_mode=subtitle_mode))
        return candidates

    def _extract_playlist_url(self, sources_payload: dict[str, Any]) -> str | None:
        raw_sources = sources_payload.get("sources")
        if isinstance(raw_sources, list):
            for source in raw_sources:
                if isinstance(source, dict):
                    candidate = str(source.get("file", "")).strip()
                    if candidate.endswith(".m3u8") or ".m3u8" in candidate:
                        return candidate
        candidate = str(sources_payload.get("file", "")).strip()
        if candidate.endswith(".m3u8") or ".m3u8" in candidate:
            return candidate
        return None

    def _extract_subtitle_url(self, sources_payload: dict[str, Any]) -> str | None:
        tracks = sources_payload.get("tracks")
        if not isinstance(tracks, list):
            return None
        for track in tracks:
            if not isinstance(track, dict):
                continue
            track_kind = str(track.get("kind", "")).lower()
            track_file = str(track.get("file", "")).strip()
            if not track_file:
                continue
            if track_kind in ("captions", "subtitles") or track_file.endswith(".vtt"):
                return track_file
        return None

    def _extract_m3u8_from_embed(self, embed_url: str) -> str | None:
        embed_page = self._fetch(self._pick_base_url(embed_url), embed_url)
        match = re.search(r"https?://[^\"']+\.m3u8[^\"']*", embed_page.text)
        if not match:
            return None
        return match.group(0)
