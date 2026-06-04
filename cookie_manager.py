import os
import threading
import time
from collections.abc import Iterable
from http.cookiejar import Cookie
from typing import Callable, Optional

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
MANAGED_BROWSER_DIR = os.path.join(BASE_DIR, "output", "browser_profile")
COOKIE_KEY = "ZHIHU_COOKIE"

_ENV_LOCK = threading.Lock()


def _ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def normalize_cookie_header(cookie_header: str) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for chunk in (cookie_header or "").split(";"):
        item = chunk.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def cookie_header_from_iterable(cookies: Iterable[Cookie | dict]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for cookie in cookies:
        if isinstance(cookie, dict):
            name = str(cookie.get("name", "")).strip()
            value = str(cookie.get("value", "")).strip()
        else:
            name = str(getattr(cookie, "name", "")).strip()
            value = str(getattr(cookie, "value", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def current_cookie_header() -> str:
    return normalize_cookie_header(os.getenv(COOKIE_KEY, ""))


def save_cookie_header(cookie_header: str, env_path: str = ENV_PATH) -> str:
    normalized = normalize_cookie_header(cookie_header)
    if not normalized:
        raise ValueError("Cookie 为空，无法保存")
    _ensure_parent(env_path)
    with _ENV_LOCK:
        lines: list[str] = []
        found = False
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.rstrip("\n")
                    if line.startswith(f"{COOKIE_KEY}="):
                        lines.append(f"{COOKIE_KEY}={normalized}")
                        found = True
                    else:
                        lines.append(line)
        if not found:
            lines.append(f"{COOKIE_KEY}={normalized}")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
    os.environ[COOKIE_KEY] = normalized
    return env_path


def clear_cookie_header(env_path: str = ENV_PATH) -> str:
    _ensure_parent(env_path)
    with _ENV_LOCK:
        lines: list[str] = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.rstrip("\n")
                    if not line.startswith(f"{COOKIE_KEY}="):
                        lines.append(line)
        with open(env_path, "w", encoding="utf-8") as f:
            if lines:
                f.write("\n".join(lines).rstrip() + "\n")
    os.environ.pop(COOKIE_KEY, None)
    return env_path


def validate_cookie_header(cookie_header: str, timeout: int = 10) -> tuple[bool, str, dict]:
    normalized = normalize_cookie_header(cookie_header)
    if not normalized:
        return False, "未检测到知乎 Cookie", {}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.zhihu.com/",
        "Origin": "https://www.zhihu.com",
        "Cookie": normalized,
    }
    try:
        response = requests.get(
            "https://www.zhihu.com/api/v4/me",
            params={"include": "name,url_token,headline"},
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return False, f"校验 Cookie 时网络请求失败: {exc}", {}

    if response.status_code != 200:
        return False, f"Cookie 校验失败，知乎返回 {response.status_code}", {}

    try:
        payload = response.json() or {}
    except ValueError:
        return False, "Cookie 校验失败，知乎返回了非 JSON 响应", {}

    name = str(payload.get("name", "")).strip()
    url_token = str(payload.get("url_token", "")).strip()
    if not name and not url_token:
        return False, "Cookie 校验失败，未识别到当前登录账号", payload

    label = name or url_token
    return True, f"Cookie 可用，当前账号：{label}", payload


def supported_browser_importers() -> dict[str, tuple[str, str]]:
    return {
        "chrome": ("Chrome", "chrome"),
        "edge": ("Edge", "edge"),
        "brave": ("Brave", "brave"),
        "chromium": ("Chromium", "chromium"),
    }


def import_cookie_from_browser(browser_name: str) -> tuple[str, str]:
    try:
        import browser_cookie3
    except ImportError as exc:
        raise RuntimeError("缺少 browser-cookie3 依赖，请先安装 requirements.txt") from exc

    browser_name = (browser_name or "").strip().lower()
    importers = supported_browser_importers()
    if browser_name not in importers:
        raise ValueError(f"暂不支持从浏览器 {browser_name} 导入 Cookie")

    label, attr = importers[browser_name]
    loader = getattr(browser_cookie3, attr, None)
    if loader is None:
        raise RuntimeError(f"browser-cookie3 未提供 {label} 的导入能力")

    try:
        jar = loader(domain_name=".zhihu.com")
    except Exception as exc:
        raise RuntimeError(f"读取 {label} 浏览器 Cookie 失败: {exc}") from exc

    header = cookie_header_from_iterable(jar)
    if not header:
        raise RuntimeError(f"没有在 {label} 中检测到知乎登录态 Cookie")
    return normalize_cookie_header(header), label


def launch_managed_login(
    progress_callback: Optional[Callable[[str], None]] = None,
    timeout_seconds: int = 900,
) -> tuple[str, dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright 依赖，请先安装 requirements.txt") from exc
    from scraper.base import BaseScraper

    log = progress_callback or (lambda _msg: None)
    os.makedirs(MANAGED_BROWSER_DIR, exist_ok=True)
    log("✓ 正在启动可见浏览器登录窗口...")
    launcher = BaseScraper()

    with sync_playwright() as playwright:
        context = launcher._launch_persistent_context(playwright, MANAGED_BROWSER_DIR, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.zhihu.com/signin", wait_until="domcontentloaded", timeout=45000)
        log("✓ 浏览器已打开，请在弹出的窗口中完成知乎登录")
        log("✓ 登录成功后程序会自动保存 Cookie，无需手动复制")

        started_at = time.monotonic()
        try:
            while True:
                if time.monotonic() - started_at > timeout_seconds:
                    raise TimeoutError("等待登录超时，请重新尝试")
                if page.is_closed():
                    raise RuntimeError("登录窗口已关闭，未能自动保存 Cookie")

                cookies = context.cookies(["https://www.zhihu.com"])
                header = cookie_header_from_iterable(cookies)
                valid, message, profile = validate_cookie_header(header)
                if valid:
                    save_cookie_header(header)
                    log(f"✓ {message}")
                    log("✓ 已自动保存 Cookie 到本地 .env")
                    return header, profile
                time.sleep(2)
        finally:
            context.close()
