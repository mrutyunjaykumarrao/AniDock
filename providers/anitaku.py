import re
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

from core.exceptions import InvalidAnimeNameError, NoSearchResultsError
from core.input_parsing import episode_sort_key
from core.models import SearchResult, ServerOption
from core.network import fetch, locked_print, set_request_referer
from providers.base import AnimeReference, ProviderAdapter, ProviderCapabilities, ResolvedStream, StreamPreferences

BASE_URL = "https://anitaku.to"
SEARCH_URL = BASE_URL + "/search.html?keyword="


class AnitakuProvider(ProviderAdapter):
    key = "anitaku"
    name = "Anitaku"
    capabilities = ProviderCapabilities(
        subtitle_modes=("SUB", "DUB", "HSUB/RAW"),
        quality_selection="HLS variants; defaults to highest when no explicit choice is requested.",
        notes="Prioritizes VibePlayer when multiple servers match the selected subtitle mode.",
    )

    def __init__(self) -> None:
        set_request_referer(BASE_URL + "/")

    def search(self, query: str) -> list[SearchResult]:
        if not query or not query.strip():
            raise InvalidAnimeNameError("Anime name can't be empty.")

        search_page = fetch(SEARCH_URL + quote_plus(query))
        search_soup = BeautifulSoup(search_page.text, "lxml")

        results: list[SearchResult] = []
        seen: set[str] = set()
        for anchor in search_soup.select("ul.items li p.name a[href]"):
            href = str(anchor.get("href", "")).strip()
            if not href:
                continue
            absolute_href = urljoin(BASE_URL, href)
            if absolute_href in seen:
                continue
            seen.add(absolute_href)
            results.append(SearchResult(title=anchor.get_text(strip=True), category_url=absolute_href))

        if not results:
            raise NoSearchResultsError("No anime found for the provided query.")

        return results

    def list_episodes(self, anime_ref: AnimeReference | str) -> list[str]:
        anime_url = anime_ref if isinstance(anime_ref, str) else anime_ref.category_url
        anime_page = fetch(anime_url)
        anime_page_soup = BeautifulSoup(anime_page.text, "lxml")

        anime_slug = anime_url.rstrip("/").split("/category/")[-1]
        expected_episode_prefix = "/" + anime_slug + "-episode-"

        episode_links: list[str] = []
        seen: set[str] = set()
        for anchor in anime_page_soup.select("a[href*='-episode-']"):
            href = str(anchor.get("href", "")).strip()
            if not href:
                continue
            if expected_episode_prefix not in href:
                continue
            absolute = urljoin(BASE_URL, href)
            if absolute in seen:
                continue
            seen.add(absolute)
            episode_links.append(absolute)

        if not episode_links:
            raise NoSearchResultsError("No episode links found on the anime page.")

        episode_links.sort(key=episode_sort_key)
        return episode_links

    def available_subtitle_modes(self, episode_ref: str) -> tuple[str, ...]:
        server_options = collect_server_options(episode_ref)
        ordered_modes = ("SUB", "DUB", "HSUB/RAW")
        discovered = tuple(mode for mode in ordered_modes if any(option.subtitle_mode == mode for option in server_options))
        return discovered or ("HSUB/RAW",)

    def resolve_stream(self, episode_ref: str, preferences: StreamPreferences | None = None) -> ResolvedStream:
        selected_preferences = preferences or StreamPreferences()
        server_options = collect_server_options(episode_ref)
        available_modes = [mode for mode in ("SUB", "DUB", "HSUB/RAW") if any(opt.subtitle_mode == mode for opt in server_options)]

        selected_mode = selected_preferences.subtitle_mode or (available_modes[0] if available_modes else "HSUB/RAW")
        if selected_preferences.subtitle_mode and selected_preferences.subtitle_mode not in available_modes and available_modes:
            selected_mode = available_modes[0]
        if (
            selected_preferences.subtitle_mode is None
            and selected_preferences.prompt_for_mode
            and selected_preferences.chooser
            and len(available_modes) > 1
        ):
            print("\nAudio/stream mode options:")
            mode_labels = {
                "SUB": "SUB (Japanese audio + subtitles)",
                "DUB": "DUB (dubbed audio when available)",
                "HSUB/RAW": "HSUB/RAW (as provided by source)",
            }
            for index, mode in enumerate(available_modes, start=1):
                print("[{0}] {1}".format(index, mode_labels.get(mode, mode)))
            mode_choice = selected_preferences.chooser(
                len(available_modes),
                "Select mode (1-{0}): ".format(len(available_modes)),
            )
            selected_mode = available_modes[mode_choice - 1]

        filtered_options = [option for option in server_options if option.subtitle_mode == selected_mode]
        if not filtered_options:
            if selected_preferences.emit_logs:
                locked_print(
                    "Requested subtitle mode '{0}' is unavailable for this episode. Falling back to available server.".format(
                        selected_mode
                    )
                )
            filtered_options = server_options

        filtered_options.sort(key=lambda option: 0 if option.provider == "VibePlayer" else 1)
        remaining_options = [
            option for option in server_options if option not in filtered_options
        ]
        remaining_options.sort(key=lambda option: 0 if option.provider == "VibePlayer" else 1)

        for option in filtered_options:
            playlist_url = extract_m3u8_from_embed_url(option.embed_url)
            if playlist_url:
                return ResolvedStream(
                    playlist_url=playlist_url,
                    subtitle_url=option.subtitle_url,
                    subtitle_mode=option.subtitle_mode,
                )

        for option in remaining_options:
            playlist_url = extract_m3u8_from_embed_url(option.embed_url)
            if playlist_url:
                if selected_preferences.emit_logs:
                    locked_print(
                        "Could not resolve playlist for requested subtitle mode '{0}'. "
                        "Falling back to available mode '{1}'.".format(selected_mode, option.subtitle_mode)
                    )
                return ResolvedStream(
                    playlist_url=playlist_url,
                    subtitle_url=option.subtitle_url,
                    subtitle_mode=option.subtitle_mode,
                )

        raise NoSearchResultsError("Could not resolve a stream playlist for episode: {0}".format(episode_ref))


def extract_m3u8_from_embed_url(embed_url: str) -> str | None:
    embed_page = fetch(embed_url)
    direct_matches = _extract_m3u8_candidates(embed_page.text)
    if direct_matches:
        return direct_matches[0]

    unpacked = _unpack_eval_packer_script(embed_page.text)
    if not unpacked:
        return None

    unpacked_candidates = _extract_m3u8_candidates(unpacked)
    if not unpacked_candidates:
        return None

    normalized_candidates = [urljoin(embed_url, candidate) for candidate in unpacked_candidates]
    normalized_candidates.sort(
        key=lambda candidate: (
            0 if "/stream/" in candidate and candidate.endswith(".m3u8") else 1,
            0 if candidate.startswith("https://") else 1,
        )
    )
    return normalized_candidates[0]


def _extract_m3u8_candidates(text: str) -> list[str]:
    pattern = r"(?:https?://[^\"']+\.m3u8[^\"']*|/[^\"']+\.m3u8[^\"']*)"
    seen: set[str] = set()
    candidates: list[str] = []
    for match in re.findall(pattern, text):
        token = str(match).strip()
        if not token or token in seen:
            continue
        seen.add(token)
        candidates.append(token)
    return candidates


def _unpack_eval_packer_script(html: str) -> str | None:
    packed_pattern = re.compile(
        r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(?P<payload>.*?)',(?P<radix>\d+),(?P<count>\d+),'(?P<symtab>.*?)'\.split\('\|'\)",
        re.DOTALL,
    )
    match = packed_pattern.search(html)
    if not match:
        return None

    payload = match.group("payload")
    radix = int(match.group("radix"))
    symbol_table = match.group("symtab").split("|")

    def replace_token(token_match: re.Match[str]) -> str:
        token = token_match.group(0)
        try:
            index = int(token, radix)
        except ValueError:
            return token
        if index >= len(symbol_table):
            return token
        replacement = symbol_table[index]
        return replacement if replacement else token

    return re.sub(r"\b\w+\b", replace_token, payload)


def extract_subtitle_url_from_embed_url(embed_url: str) -> str | None:
    parsed_url = urlparse(embed_url)
    params = parse_qs(parsed_url.query)
    for key in ("sub", "caption_1", "c1_file"):
        if key in params and params[key]:
            return params[key][0]
    return None


def get_server_provider(embed_url: str) -> str:
    parsed_url = urlparse(embed_url)
    host = parsed_url.netloc.lower()
    if "vibeplayer.site" in host:
        return "VibePlayer"
    if "otakuhg.site" in host:
        return "StreamHG"
    if "otakuvid.online" in host:
        return "Earnvids"
    if "myvidplay.com" in host:
        return "Doodstream"
    return host or "Unknown"


def collect_server_options(episode_url: str) -> list[ServerOption]:
    episode_page = fetch(episode_url)
    episode_soup = BeautifulSoup(episode_page.text, "lxml")

    tab_mode_by_key: dict[str, str] = {}
    for tab_label in episode_soup.select(".servers-tab .name_type[data-type]"):
        tab_type = str(tab_label.get("data-type", "")).strip().upper()
        class_names = tab_label.get("class", [])
        tab_key = next((name for name in class_names if isinstance(name, str) and name.startswith("tab_")), "")
        if not tab_key:
            continue
        if "DUB" in tab_type:
            tab_mode_by_key[tab_key] = "DUB"
        elif tab_type == "SUB":
            tab_mode_by_key[tab_key] = "SUB"
        elif "HSUB" in tab_type or "RAW" in tab_type:
            tab_mode_by_key[tab_key] = "HSUB/RAW"

    raw_options: list[dict] = []
    tab_has_subtitles: dict[str, bool] = {}
    for server_link in episode_soup.select("a.server-video[data-video]"):
        embed_url = str(server_link.get("data-video", "")).strip()
        if not embed_url:
            continue
        subtitle_url = extract_subtitle_url_from_embed_url(embed_url)
        tab_key = str(server_link.get("data-tab", "")).strip()
        if tab_key:
            tab_has_subtitles[tab_key] = tab_has_subtitles.get(tab_key, False) or bool(subtitle_url)
        raw_options.append(
            {
                "label": " ".join(server_link.get_text(" ", strip=True).split()),
                "embed_url": embed_url,
                "subtitle_url": subtitle_url,
                "provider": get_server_provider(embed_url),
                "tab_key": tab_key,
            }
        )

    has_multi_tabs = len([key for key in tab_has_subtitles if key]) > 1
    options: list[ServerOption] = []
    for raw in raw_options:
        subtitle_mode = tab_mode_by_key.get(raw["tab_key"], "HSUB/RAW")
        if raw["tab_key"] not in tab_mode_by_key:
            if raw["subtitle_url"]:
                subtitle_mode = "SUB"
            elif has_multi_tabs and raw["tab_key"]:
                if tab_has_subtitles.get(raw["tab_key"], False):
                    subtitle_mode = "SUB"
                else:
                    subtitle_mode = "DUB"
        options.append(
            ServerOption(
                label=raw["label"],
                embed_url=raw["embed_url"],
                subtitle_url=raw["subtitle_url"],
                provider=raw["provider"],
                subtitle_mode=subtitle_mode,
            )
        )

    if not options:
        raise NoSearchResultsError("No streaming servers found for episode: {0}".format(episode_url))

    return options
