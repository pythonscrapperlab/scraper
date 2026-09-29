"""Browser automation layer using Playwright for async JavaScript rendering."""

from typing import Any, Dict, Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from aevorex.config import settings


class PlaywrightBrowser:
    """
    Async Playwright wrapper for JavaScript-heavy sites.
    
    Usage:
        async with PlaywrightBrowser() as browser:
            html = await browser.fetch_html("https://example.com")
            json_data = await browser.fetch_json("https://example.com", selector=".data")
    """

    def __init__(self):
        """Initialize Playwright browser."""
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None

    async def __aenter__(self):
        """Context manager entry — launch browser."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit — close browser."""
        await self.close()

    async def start(self) -> None:
        """Launch browser."""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=settings.playwright_headless,
        )
        self.context = await self.browser.new_context(
            user_agent=settings.scraper_user_agent,
        )

    async def close(self) -> None:
        """Close browser."""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def fetch_html(
        self,
        url: str,
        wait_until: Optional[str] = None,
        timeout: Optional[int] = None,
        wait_for_selector: Optional[str] = None,
    ) -> Optional[str]:
        """
        Fetch HTML content after JavaScript rendering.
        
        Args:
            url: URL to fetch
            wait_until: 'load' | 'domcontentloaded' | 'networkidle'
            timeout: Timeout in milliseconds
            wait_for_selector: CSS selector to wait for before returning
            
        Returns:
            HTML string or None on failure
        """
        page: Optional[Page] = None
        try:
            page = await self.context.new_page()
            await page.goto(
                url,
                wait_until=wait_until or settings.playwright_wait_for,
                timeout=timeout or settings.playwright_timeout,
            )

            if wait_for_selector:
                await page.wait_for_selector(wait_for_selector, timeout=timeout or settings.playwright_timeout)

            html = await page.content()
            return html

        except Exception:
            raise
        finally:
            if page:
                await page.close()

    async def fetch_json(
        self,
        url: str,
        selector: Optional[str] = None,
        script_attr: str = "type",
        script_value: str = "application/json",
        wait_until: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch JSON data embedded in page (e.g., __NEXT_DATA__ or script tags).
        
        Args:
            url: URL to fetch
            selector: CSS selector for script tag (defaults to finding JSON script tags)
            script_attr: Attribute to match (e.g., 'type' for type=application/json)
            script_value: Attribute value to match
            wait_until: 'load' | 'domcontentloaded' | 'networkidle'
            timeout: Timeout in milliseconds
            
        Returns:
            Parsed JSON dict or None on failure
        """
        page: Optional[Page] = None
        try:
            page = await self.context.new_page()
            await page.goto(
                url,
                wait_until=wait_until or settings.playwright_wait_for,
                timeout=timeout or settings.playwright_timeout,
            )

            json_data = await page.evaluate(
                f"""
                () => {{
                    const selector = '{selector or f'script[{script_attr}="{script_value}"]'}';
                    const script = document.querySelector(selector);
                    if (!script) return null;
                    try {{
                        return JSON.parse(script.textContent);
                    }} catch (e) {{
                        return null;
                    }}
                }}
                """
            )
            return json_data

        except Exception:
            raise
        finally:
            if page:
                await page.close()

    async def fetch_text_from_selector(
        self,
        url: str,
        selector: str,
        wait_until: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> Optional[str]:
        """
        Fetch text content from a specific selector.
        
        Args:
            url: URL to fetch
            selector: CSS selector
            wait_until: Load strategy
            timeout: Timeout in milliseconds
            
        Returns:
            Text content or None
        """
        page: Optional[Page] = None
        try:
            page = await self.context.new_page()
            await page.goto(
                url,
                wait_until=wait_until or settings.playwright_wait_for,
                timeout=timeout or settings.playwright_timeout,
            )
            await page.wait_for_selector(selector, timeout=timeout or settings.playwright_timeout)

            text = await page.text_content(selector)
            return text

        except Exception:
            raise
        finally:
            if page:
                await page.close()

    async def fetch_all_text_from_selector(
        self,
        url: str,
        selector: str,
        wait_until: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> Optional[list]:
        """
        Fetch text content from all matching selectors.
        
        Args:
            url: URL to fetch
            selector: CSS selector
            wait_until: Load strategy
            timeout: Timeout in milliseconds
            
        Returns:
            List of text content or None
        """
        page: Optional[Page] = None
        try:
            page = await self.context.new_page()
            await page.goto(
                url,
                wait_until=wait_until or settings.playwright_wait_for,
                timeout=timeout or settings.playwright_timeout,
            )

            texts = await page.locator(selector).all_text_contents()
            return texts

        except Exception:
            raise
        finally:
            if page:
                await page.close()
