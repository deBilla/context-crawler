"""HTTP page fetcher using httpx.

Fetches a URL and returns the response content. Handles timeouts, retries,
and response size limits. Uses a shared httpx.AsyncClient for connection pooling.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import httpx

from crawler.config import CrawlerConfig

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """Result of fetching a single URL."""
    url: str
    status_code: int
    content: bytes
    content_hash: str
    content_type: str
    final_url: str  # After redirects


class FetchError(Exception):
    """Failed to fetch a URL after retries."""
    def __init__(self, url: str, reason: str):
        self.url = url
        self.reason = reason
        super().__init__(f"Failed to fetch {url}: {reason}")


class PageFetcher:
    """Async HTTP fetcher with retry and size limits.

    Reuses a single httpx.AsyncClient for connection pooling across requests.
    Supports optional Puppeteer rendering for JS-heavy pages.
    """

    def __init__(self, client: httpx.AsyncClient, config: CrawlerConfig) -> None:
        self.client = client
        self.config = config

    async def fetch_with_puppeteer(self, url: str, expand: bool = False) -> FetchResult:
        """Fetch a URL using the Puppeteer sidecar for JS rendering.

        `expand` asks the sidecar to scroll and click through "load more"
        before returning. Banks that paginate their catalogue — HNB shows ten
        of several hundred promotions on first paint — otherwise report a
        fraction of what they carry.
        """
        try:
            response = await self.client.post(
                f"{self.config.puppeteer_url}/render",
                json={
                    "url": url,
                    "timeout": int(self.config.timeout * 1000),
                    "expand": expand,
                },
                timeout=httpx.Timeout(connect=10.0, read=self.config.timeout + 10, write=10.0, pool=10.0),
            )
        except httpx.TimeoutException:
            raise FetchError(url, "puppeteer timeout")
        except httpx.ConnectError as e:
            raise FetchError(url, f"puppeteer connection error: {e}")

        if response.status_code != 200:
            raise FetchError(url, f"puppeteer returned {response.status_code}")

        data = response.json()
        content = data["html"].encode("utf-8")

        if len(content) > self.config.max_response_size:
            raise FetchError(url, f"response too large: {len(content)} bytes")

        content_hash = hashlib.sha256(content).hexdigest()

        return FetchResult(
            url=url,
            status_code=200,
            content=content,
            content_hash=content_hash,
            content_type="text/html; charset=utf-8",
            final_url=data.get("final_url", url),
        )

    async def fetch(self, url: str) -> FetchResult:
        """Fetch a URL and return the response.

        Raises FetchError if the request fails after retries or exceeds size limits.
        """
        try:
            response = await self.client.get(
                url,
                headers={
                    "User-Agent": self.config.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                follow_redirects=True,
            )
        except httpx.TimeoutException:
            raise FetchError(url, "timeout")
        except httpx.ConnectError as e:
            raise FetchError(url, f"connection error: {e}")
        except httpx.TooManyRedirects:
            raise FetchError(url, "too many redirects")

        # Check response size
        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > self.config.max_response_size:
            raise FetchError(url, f"response too large: {content_length} bytes")

        content = response.content
        if len(content) > self.config.max_response_size:
            raise FetchError(url, f"response too large: {len(content)} bytes")

        content_hash = hashlib.sha256(content).hexdigest()

        return FetchResult(
            url=url,
            status_code=response.status_code,
            content=content,
            content_hash=content_hash,
            content_type=response.headers.get("content-type", ""),
            final_url=str(response.url),
        )


def create_http_client(config: CrawlerConfig) -> httpx.AsyncClient:
    """Create a configured httpx client for the crawler."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=10.0,
            read=config.timeout,
            write=10.0,
            pool=10.0,
        ),
        limits=httpx.Limits(
            max_connections=config.max_concurrent_requests * 2,
            max_keepalive_connections=config.max_concurrent_requests,
        ),
        follow_redirects=True,
        max_redirects=5,
        http2=True,
    )
