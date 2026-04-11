import threading
import time

import requests

REQUEST_TIMEOUT_SECONDS = 25
MAX_FETCH_RETRIES = 4
RETRY_BACKOFF_SECONDS = 1.2
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
SESSION_LOCAL = threading.local()
PRINT_LOCK = threading.Lock()
RETRY_LOGGING_ENABLED = True
REQUEST_REFERER: str | None = None


def set_retry_logging_enabled(enabled: bool) -> None:
    global RETRY_LOGGING_ENABLED
    RETRY_LOGGING_ENABLED = enabled


def set_request_referer(referer: str | None) -> None:
    global REQUEST_REFERER
    REQUEST_REFERER = referer
    session = getattr(SESSION_LOCAL, "session", None)
    if session is None:
        return
    if referer:
        session.headers["Referer"] = referer
    else:
        session.headers.pop("Referer", None)


def get_session() -> requests.Session:
    session = getattr(SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        if REQUEST_REFERER:
            session.headers["Referer"] = REQUEST_REFERER
        SESSION_LOCAL.session = session
    return session


def fetch(url: str) -> requests.Response:
    last_error: requests.RequestException | None = None
    for attempt in range(1, MAX_FETCH_RETRIES + 1):
        try:
            response = get_session().get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt == MAX_FETCH_RETRIES:
                break
            sleep_time = RETRY_BACKOFF_SECONDS * attempt
            if RETRY_LOGGING_ENABLED:
                with PRINT_LOCK:
                    print(
                        "Retrying fetch ({0}/{1}) for {2} in {3:.1f}s: {4}".format(
                            attempt, MAX_FETCH_RETRIES, url, sleep_time, exc
                        )
                    )
            time.sleep(sleep_time)

    raise requests.RequestException(
        "Failed to fetch {0} after {1} attempts: {2}".format(url, MAX_FETCH_RETRIES, last_error)
    ) from last_error


def locked_print(message: str) -> None:
    with PRINT_LOCK:
        print(message)
