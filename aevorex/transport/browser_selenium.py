"""Browser automation layer using Selenium (synchronous fallback)."""

import asyncio
from typing import Any, Dict, List, Optional

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from aevorex.config import settings


class SeleniumBrowser:
    """
    Synchronous Selenium wrapper (fallback for Playwright-resistant sites).
    
    Runs sync WebDriver operations in thread pool to avoid blocking async event loop.
    
    Usage:
        async with SeleniumBrowser() as browser:
            html = await browser.fetch_html("https://example.com")
    """

    def __init__(self):
        """Initialize Selenium browser."""
        self.driver: Optional[webdriver.Chrome] = None

    async def __aenter__(self):
        """Context manager entry — launch browser."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit — close browser."""
        await self.close()

    async def start(self) -> None:
        """Launch browser in thread pool."""
        loop = asyncio.get_event_loop()
        self.driver = await loop.run_in_executor(None, self._init_driver)

    def _init_driver(self) -> webdriver.Chrome:
        """Initialize WebDriver (runs in thread pool)."""
        options = webdriver.ChromeOptions()
        if settings.selenium_headless:
            options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument(f"user-agent={settings.scraper_user_agent}")

        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(settings.selenium_timeout)
        driver.implicitly_wait(10)
        return driver

    async def close(self) -> None:
        """Close browser in thread pool."""
        if self.driver:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.driver.quit)
            self.driver = None

    async def fetch_html(self, url: str, wait_for_selector: Optional[str] = None) -> Optional[str]:
        """
        Fetch HTML content.
        
        Args:
            url: URL to fetch
            wait_for_selector: CSS selector to wait for before returning
            
        Returns:
            HTML string or None on failure
        """
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, self._fetch_html_sync, url, wait_for_selector
            )
        except Exception:
            raise

    def _fetch_html_sync(self, url: str, wait_for_selector: Optional[str]) -> str:
        """Fetch HTML (runs in thread pool)."""
        self.driver.get(url)

        if wait_for_selector:
            WebDriverWait(self.driver, settings.selenium_timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_for_selector))
            )

        return self.driver.page_source

    async def fetch_json(
        self, url: str, script_selector: str = 'script[type="application/json"]'
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch JSON from script tag.
        
        Args:
            url: URL to fetch
            script_selector: CSS selector for script tag
            
        Returns:
            Parsed JSON or None
        """
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, self._fetch_json_sync, url, script_selector
            )
        except Exception:
            raise

    def _fetch_json_sync(self, url: str, script_selector: str) -> Optional[Dict[str, Any]]:
        """Fetch JSON (runs in thread pool)."""
        self.driver.get(url)

        try:
            script_element = self.driver.find_element(By.CSS_SELECTOR, script_selector)
            json_text = script_element.get_attribute("textContent")
            import json

            return json.loads(json_text)
        except Exception:
            return None

    async def fetch_text_from_selector(self, url: str, selector: str) -> Optional[str]:
        """
        Fetch text from selector.
        
        Args:
            url: URL to fetch
            selector: CSS selector
            
        Returns:
            Text content or None
        """
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._fetch_text_sync, url, selector)
        except Exception:
            raise

    def _fetch_text_sync(self, url: str, selector: str) -> Optional[str]:
        """Fetch text (runs in thread pool)."""
        self.driver.get(url)

        try:
            element = WebDriverWait(self.driver, settings.selenium_timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            return element.text
        except Exception:
            return None

    async def fetch_all_text_from_selector(self, url: str, selector: str) -> Optional[List[str]]:
        """
        Fetch text from all matching selectors.
        
        Args:
            url: URL to fetch
            selector: CSS selector
            
        Returns:
            List of text content or None
        """
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, self._fetch_all_text_sync, url, selector
            )
        except Exception:
            raise

    def _fetch_all_text_sync(self, url: str, selector: str) -> List[str]:
        """Fetch all text (runs in thread pool)."""
        self.driver.get(url)

        try:
            elements = WebDriverWait(self.driver, settings.selenium_timeout).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector))
            )
            return [el.text for el in elements]
        except Exception:
            return []
