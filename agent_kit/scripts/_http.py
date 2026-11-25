"""HTTP details shared by the kit's own V2 and account clients."""

from importlib.metadata import PackageNotFoundError, version

import aiohttp

REGION_BLOCK_MARKER = "Not available in your region"
REGION_UNAVAILABLE_MESSAGE = (
    "Reya's API is not available from your region (HTTP 403 regional block); "
    "nothing was sent to the venue. See docs/private-first-use.md."
)


def _kit_version() -> str:
    try:
        return version("reya-trading-agent-kit")
    except PackageNotFoundError:
        return "unknown"


# Keep aiohttp's default product token (the API's edge accepts it) and identify the kit after it.
USER_AGENT = f"{aiohttp.http.SERVER_SOFTWARE} reya-trading-agent-kit/{_kit_version()}"


class RegionUnavailableError(ValueError):
    """The API edge refused this region; the request never reached the venue."""

    def __init__(self):
        super().__init__(REGION_UNAVAILABLE_MESSAGE)


def region_blocked(status, body) -> bool:
    if status != 403 or body is None:
        return False
    text = body.decode("utf-8", "ignore") if isinstance(body, (bytes, bytearray)) else str(body)
    return REGION_BLOCK_MARKER in text[:65536]
