"""Opt-in update notification (ADR-008).

Off unless the user turns it on. At most once a day a packaged build asks
GitHub for the latest published release's version number and, when it differs
from the running version, the app shows a link to that release page. Nothing is
downloaded or run, no identifiers are sent, and every failure is silent.
"""
import json
import re
import urllib.request
from typing import Any, Callable, Optional, Tuple

from .runtime import APP_VERSION, logger

__all__ = ["RELEASES_API", "release_url", "parse_version", "check_due",
           "recommended_version", "fetch_latest_tag"]

RELEASES_API = "https://api.github.com/repos/roko-tech/Anime-Subs-Customizer/releases/latest"
# The only page the app ever opens for an update: this fixed prefix plus a
# validated vX.Y.Z tag. The API's html_url is deliberately ignored.
_RELEASE_PAGE = "https://github.com/roko-tech/Anime-Subs-Customizer/releases/tag/"
_TAG = re.compile(r"^v(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
CHECK_INTERVAL = 24 * 60 * 60


def parse_version(tag: Any) -> Optional[Tuple[int, int, int]]:
    """(major, minor, patch) for a "vX.Y.Z" tag, else None."""
    m = _TAG.match(tag) if isinstance(tag, str) else None
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


def release_url(tag: str) -> Optional[str]:
    return _RELEASE_PAGE + tag if parse_version(tag) else None


def check_due(enabled: bool, last_check: float, now: float) -> bool:
    """Only when the user opted in, and at most once a day (a clock moved
    backwards counts as due, so a bad timestamp can't block checks forever)."""
    return bool(enabled) and not (0 <= now - (last_check or 0) < CHECK_INTERVAL)


def recommended_version(latest_tag: Any, current: str = APP_VERSION) -> Optional[str]:
    """The tag to recommend, or None. Any difference counts, not just "newer":
    when a bad release is rolled back by re-pointing "latest" to an older good
    one, users of the bad one are pointed back to it too."""
    latest = parse_version(latest_tag)
    if latest is None:
        return None
    return latest_tag if latest != parse_version(f"v{current}") else None


def fetch_latest_tag(timeout: float = 10.0,
                     opener: Callable[..., Any] = urllib.request.urlopen) -> Optional[str]:
    """The latest release's tag from the GitHub API, or None on ANY problem
    (offline, rate limited, unexpected reply). Runs on a background thread."""
    request = urllib.request.Request(RELEASES_API, headers={
        "User-Agent": f"AnimeSubsCustomizer/{APP_VERSION}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with opener(request, timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                return None
            data = json.loads(response.read(65536).decode("utf-8"))
        tag = data.get("tag_name") if isinstance(data, dict) else None
        return tag if parse_version(tag) else None
    except Exception as e:  # noqa: BLE001 — never bother the user about this
        logger.info(f"Update check skipped: {e}")
        return None
