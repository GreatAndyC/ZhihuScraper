import requests
import random
import time
import logging
from typing import Optional, Dict, Any, Callable
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    BROWSER_DELAY_MAX,
    BROWSER_DELAY_MIN,
    CONSERVATIVE_BROWSER_DELAY_MAX,
    CONSERVATIVE_BROWSER_DELAY_MIN,
    CONSERVATIVE_REQUEST_DELAY_MAX,
    CONSERVATIVE_REQUEST_DELAY_MIN,
    COOKIE,
    MAX_RETRIES,
    REQUEST_DELAY_MAX,
    REQUEST_DELAY_MIN,
    REQUEST_TIMEOUT,
)

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


class BaseScraper:
    def __init__(self, cookie: Optional[str] = None, conservative_mode: bool = False):
        self.cookie = cookie or COOKIE
        self.conservative_mode = conservative_mode
        self.session = self._create_session()
        self._playwright_browser = None

    def _create_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=MAX_RETRIES,
            connect=MAX_RETRIES,
            read=MAX_RETRIES,
            status=MAX_RETRIES,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "POST"]),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.zhihu.com/",
            "Origin": "https://www.zhihu.com",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        if extra:
            headers.update(extra)
        return headers

    def _delay(self):
        if self.conservative_mode:
            delay = random.uniform(CONSERVATIVE_REQUEST_DELAY_MIN, CONSERVATIVE_REQUEST_DELAY_MAX)
        else:
            delay = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
        time.sleep(delay)

    def _browser_delay(self):
        if self.conservative_mode:
            delay = random.uniform(CONSERVATIVE_BROWSER_DELAY_MIN, CONSERVATIVE_BROWSER_DELAY_MAX)
        else:
            delay = random.uniform(BROWSER_DELAY_MIN, BROWSER_DELAY_MAX)
        time.sleep(delay)

    def _request(
        self, method: str, url: str, **kwargs
    ) -> requests.Response:
        self._delay()
        extra_headers = kwargs.pop("headers", None)
        timeout = kwargs.pop("timeout", REQUEST_TIMEOUT)
        headers = self._headers(extra_headers)
        logger.info(f"{method} {url}")
        return self.session.request(method, url, headers=headers, timeout=timeout, **kwargs)

    def get(self, url: str, **kwargs) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self._request("POST", url, **kwargs)

    def get_with_playwright(self, url: str, selector: str = "h1", timeout: int = 10000) -> Optional[str]:
        """Fallback: use Playwright to fetch page content (requires JS rendering)."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("Playwright not installed, cannot use fallback")
            return None

        if self._playwright_browser is None:
            self._playwright_browser = sync_playwright().start().chromium.launch(args=["--no-sandbox"])

        context = self._playwright_browser.contexts[0] if self._playwright_browser.contexts else self._playwright_browser.new_context(
            user_agent=random.choice(USER_AGENTS)
        )
        page = context.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            if resp and resp.status == 200:
                page.wait_for_selector(selector, timeout=timeout)
                return page.content()
        finally:
            page.close()
        return None

    def close(self):
        if self._playwright_browser:
            self._playwright_browser.close()
            self._playwright_browser = None
