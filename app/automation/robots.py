"""robots.txt compliance.

Before visiting a URL the engine asks ``RobotsPolicy.check``. The robots.txt of each origin is
fetched once (through the browser's request context, so it uses the same network path) and
cached for ``ttl_s`` seconds.

* 200      - rules are parsed and applied to our user agent
* 4xx      - no rules published: everything is allowed (standard behaviour)
* 5xx/net  - transient error: raised as retryable ``NavigationError`` rather than guessing
"""

from __future__ import annotations

import time
import urllib.robotparser
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from app.core.errors import NavigationError, RobotsDisallowedError
from app.core.logging import get_logger

log = get_logger(__name__)

# fetch(url) -> (status, body)
Fetcher = Callable[[str], Awaitable[tuple[int, str]]]


class RobotsPolicy:
    def __init__(self, user_agent: str, *, ttl_s: float = 600.0, enabled: bool = True) -> None:
        self.user_agent = user_agent
        self.ttl_s = ttl_s
        self.enabled = enabled
        self._cache: dict[str, tuple[float, urllib.robotparser.RobotFileParser]] = {}

    @staticmethod
    def origin(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    async def _parser_for(self, url: str, fetch: Fetcher) -> urllib.robotparser.RobotFileParser:
        origin = self.origin(url)
        cached = self._cache.get(origin)
        if cached and time.monotonic() - cached[0] < self.ttl_s:
            return cached[1]
        robots_url = origin + "/robots.txt"
        try:
            status, body = await fetch(robots_url)
        except Exception as exc:
            raise NavigationError("Could not fetch robots.txt.", details={"url": robots_url}) from exc
        parser = urllib.robotparser.RobotFileParser(robots_url)
        if status == 200:
            parser.parse(body.splitlines())
        elif 400 <= status < 500:
            parser.allow_all = True  # type: ignore[attr-defined]  # stdlib attribute missing from typeshed
        else:
            raise NavigationError("robots.txt returned a server error.", details={"url": robots_url, "status": status})
        self._cache[origin] = (time.monotonic(), parser)
        log.info("robots.loaded", extra={"origin": origin, "status": status})
        return parser

    async def check(self, url: str, fetch: Fetcher) -> None:
        """Raise ``RobotsDisallowedError`` if robots.txt forbids ``url`` for our user agent."""
        if not self.enabled:
            return
        parser = await self._parser_for(url, fetch)
        if not parser.can_fetch(self.user_agent, url):
            raise RobotsDisallowedError(
                "robots.txt disallows this URL; the automation will not visit it.", details={"url": url}
            )
