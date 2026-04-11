#!/usr/bin/env python3

import requests

from core.exceptions import InvalidAnimeNameError, NoSearchResultsError, ProviderError
from cli.app import main
from core.models import DownloadPausedError


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nGoodbye! Exiting AniDock gracefully.")
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
