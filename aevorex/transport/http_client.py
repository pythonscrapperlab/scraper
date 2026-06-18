"""HTTP transport layer using httpx with retries and resilience."""

import asyncio
from typing import Any, Dict, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential
from aevorex.transport.proxy_manager import ProxyManager

from aevorex.config import settings


class HttpClient:
    """
    Async HTTP client wrapper with retries, timeouts, and headers.
    
    Usage:
        client = HttpClient()
        response = await client.get("https://example.com")
        json_data = await client.get_json("https://api.example.com/data")
    """

    def __init__(self, headers={}, **kwargs):
        """
        Initialize HTTP client.
        
        Args:
            **kwargs: Passed to httpx.AsyncClient (timeout, proxies, etc.)
        """
        timeout = httpx.Timeout(settings.scraper_timeout)
        if not headers:
            headers = {
                "User-Agent": settings.scraper_user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }
            
        self.client = httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            follow_redirects=True,
            **kwargs,
        )

    async def __aenter__(self):
        """Context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit — close client."""
        await self.close()

    async def close(self) -> None:
        """Close the client and cleanup."""
        await self.client.aclose()

    @retry(
        stop=stop_after_attempt(settings.scraper_retries),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def get(
        self, url: str, headers: Optional[Dict[str, str]] = None, **kwargs
    ) -> Optional[httpx.Response]:
        """
        GET request with retries.
        
        Args:
            url: URL to fetch
            headers: Optional additional headers
            **kwargs: Passed to httpx.AsyncClient.get()
            
        Returns:
            httpx.Response or None on failure
        """
        try:
            merged_headers = dict(self.client.headers)
            if headers:
                merged_headers.update(headers)

            response = await self.client.get(url, headers=merged_headers, **kwargs)
            response.raise_for_status()
            return response

        except Exception:
            raise

    @retry(
        stop=stop_after_attempt(settings.scraper_retries),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def post(
        self,
        url: str,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> Optional[httpx.Response]:
        """
        POST request with retries.
        
        Args:
            url: URL to post to
            json: JSON payload
            headers: Optional additional headers
            **kwargs: Passed to httpx.AsyncClient.post()
            
        Returns:
            httpx.Response or None on failure
        """
        try:
            merged_headers = dict(self.client.headers)
            if headers:
                merged_headers.update(headers)

            response = await self.client.post(
                url, json=json, headers=merged_headers, **kwargs
            )
            response.raise_for_status()
            return response

        except Exception:
            raise

    async def get_text(
        self, url: str, headers: Optional[Dict[str, str]] = None, **kwargs
    ) -> Optional[str]:
        """GET and return text."""
        try:
            response = await self.get(url, headers=headers, **kwargs)
            return response.text if response else None
        except Exception as e:
            raise

    async def get_json(
        self, url: str, headers: Optional[Dict[str, str]] = None, **kwargs
    ) -> Optional[Dict[str, Any]]:
        """GET and return JSON."""
        try:
            response = await self.get(url, headers=headers, **kwargs)
            return response.json() if response else None
        except Exception as e:
            raise


# Global instance for convenience
_http_client: Optional[HttpClient] = None


async def get_http_client() -> HttpClient:
    """Get or create global HTTP client."""
    global _http_client
    if _http_client is None:
        _http_client = HttpClient()
    return _http_client


async def close_http_client() -> None:
    """Close global HTTP client."""
    global _http_client
    if _http_client:
        await _http_client.close()
        _http_client = None
