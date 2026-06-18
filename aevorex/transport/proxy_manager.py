"""Proxy manager for rotation and fallback (disabled by default)."""

from typing import Optional

from aevorex.config import settings


class ProxyManager:
    """
    Manages proxy rotation across providers.
    
    Supported providers:
    - brightdata (residential IPs)
    - oxylabs (residential/datacenter)
    - smartproxy (residential)
    
    Disabled by default via PROXY_ENABLED=false in .env
    
    Usage:
        proxy_mgr = ProxyManager()
        if proxy_mgr.enabled:
            proxy_url = proxy_mgr.get_next_proxy()
        else:
            proxy_url = None  # Direct connection
    """

    def __init__(self):
        """Initialize proxy manager."""
        self.enabled = settings.proxy_enabled
        self.provider = settings.proxy_provider
        self.user = settings.proxy_user
        self.password = settings.proxy_password
        self.zone = settings.proxy_zone
        self.proxy_list = []
        self.current_index = 0
        self._initialized = False

    async def init_proxies(self) -> None:
        """Initialize proxy list based on provider."""
        if not self.enabled or self._initialized:
            return

        if self.provider == "brightdata":
            await self._init_brightdata()
        elif self.provider == "oxylabs":
            await self._init_oxylabs()
        elif self.provider == "smartproxy":
            await self._init_smartproxy()
        elif self.provider == "webshare":
            await self._init_webshare()
        
        self._initialized = True

    async def _init_brightdata(self) -> None:
        """Initialize Brightdata proxy list."""
        if not self.user or not self.password or not self.zone:
            raise ValueError("Brightdata requires user, password, and zone")

        # Format: http://username-zone:password@proxy.provider.com:port
        proxy_url = (
            f"http://{self.user}-zone-{self.zone}:{self.password}@"
            f"proxy.provider.com:8080"
        )
        self.proxy_list = [proxy_url]

    async def _init_oxylabs(self) -> None:
        """Initialize Oxylabs proxy list."""
        if not self.user or not self.password:
            raise ValueError("Oxylabs requires user and password")

        proxy_url = (
            f"http://{self.user}:{self.password}@"
            f"pr.oxylabs.io:7777"
        )
        self.proxy_list = [proxy_url]

    async def _init_smartproxy(self) -> None:
        """Initialize SmartProxy list."""
        if not self.user or not self.password:
            raise ValueError("SmartProxy requires user and password")

        proxy_url = (
            f"http://{self.user}:{self.password}@"
            f"gate.smartproxy.com:7000"
        )
        self.proxy_list = [proxy_url]
        
    async def _init_webshare(self) -> None:
        """Initialize Webshare proxy list."""
        if not self.user or not self.password:
            raise ValueError("Webshare requires user and password")

        proxy_url = (
            f"http://{self.user}:{self.password}@"
            f"p.webshare.io:80"
        )
        self.proxy_list = [proxy_url]

    async def get_next_proxy(self) -> Optional[str]:
        """Get next proxy in rotation."""
        if not self.enabled:
            return None

        if not self._initialized:
            await self.init_proxies()

        if not self.proxy_list:
            return None

        proxy = self.proxy_list[self.current_index % len(self.proxy_list)]
        self.current_index += 1
        return proxy

    def get_httpx_proxies(self) -> Optional[dict]:
        """Get proxies dict for httpx.AsyncClient."""
        proxy = self.get_next_proxy()
        if not proxy:
            return None
        return {"http://": proxy, "https://": proxy}
    
    async def get_httpx_proxy(self) -> Optional[str]:
        """Get a proxy URL string suitable for httpx's `proxy=` kwarg."""
        return await self.get_next_proxy()

    def get_selenium_proxy(self) -> Optional[dict]:
        """Get proxy for Selenium WebDriver."""
        proxy = self.get_next_proxy()
        if not proxy:
            return None
        return {"proxyType": "MANUAL", "httpProxy": proxy, "sslProxy": proxy}

    def get_rotating_proxy_url(self) -> Optional[str]:
        """
        Return a single proxy URL for webshare's rotating residential gateway.
        Suitable for reuse across an entire HttpClient session — no per-request
        rotation needed since the gateway itself rotates exit IPs.
        """
        if not self.enabled:
            return None
        if self.provider != "webshare":
            return None
        if not self.user or not self.password:
            return None
        return f"http://{self.user}:{self.password}@p.webshare.io:80"