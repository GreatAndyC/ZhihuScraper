import base64
import hashlib
import os
import random
import re
import time
from datetime import datetime
from html import escape
from urllib.parse import urlparse

import requests

from config import (
    ASSET_DOWNLOAD_DELAY_MAX,
    ASSET_DOWNLOAD_DELAY_MIN,
    CONSERVATIVE_ASSET_DOWNLOAD_DELAY_MAX,
    CONSERVATIVE_ASSET_DOWNLOAD_DELAY_MIN,
    OUTPUT_DIR,
)
from export_utils import question_export_stem, user_export_stem

ZHIMG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.zhihu.com/",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}

IMG_URL_RE = re.compile(
    r"""(?<![\w-])(?P<name>src|data-original|data-actualsrc|data-src)=(?P<quote>["'])(?P<url>https?://[^"' ]+)(?P=quote)""",
    re.IGNORECASE,
)
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
IMG_ATTR_RE = re.compile(
    r"""(?<![\w-])(?P<name>src|data-original|data-actualsrc|data-src|srcset)=(?P<quote>["'])(?P<value>.*?)(?P=quote)""",
    re.IGNORECASE,
)
LAZY_PLACEHOLDER_RE = re.compile(r"^data:image/(?:svg\+xml|gif)", re.IGNORECASE)


def _format_time(value) -> str:
    if not value:
        return "未知时间"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return escape(str(value))


def _timestamp(value) -> int:
    if not value:
        return 0
    if isinstance(value, datetime):
        return int(value.timestamp())
    return 0


def _format_duration(seconds: float) -> str:
    total_seconds = max(1, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分")
    if secs or not parts:
        parts.append(f"{secs}秒")
    return "".join(parts)


def _initial(name: str) -> str:
    value = (name or "").strip()
    return value[:1] or "知"


def _safe_filename(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "-", name).strip("-") or "item"


def _guess_extension(url: str, content_type: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or ""
    if "." in os.path.basename(path):
        ext = os.path.splitext(path)[1].lower()
        if 1 < len(ext) <= 8:
            return ext
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/svg+xml": ".svg",
        "image/avif": ".avif",
    }
    return mapping.get((content_type or "").split(";")[0].lower(), ".jpg")


def _avatar_markup(src: str, alt: str, fallback_text: str) -> str:
    if src:
        return (
            f'<span class="avatar-frame"><img class="avatar-img" src="{escape(src)}" '
            f'alt="{escape(alt)}" loading="lazy" /></span>'
        )
    return f'<span class="avatar-fallback">{escape(fallback_text)}</span>'


def _content_block(html: str, text: str, mode: str) -> str:
    if mode in {"text", "fast"}:
        if not text:
            return '<div class="content empty">没有可显示的正文。</div>'
        return f'<div class="content text-only">{escape(text)}</div>'
    if html:
        return f'<div class="content">{html}</div>'
    if text:
        return f'<div class="content text-only">{escape(text)}</div>'
    return '<div class="content empty">没有可显示的正文。</div>'


def _page_script(enable_type_filter: bool) -> str:
    filter_block = """
    const typeFilter = document.getElementById('type-filter');
    const selectedType = typeFilter ? typeFilter.value : 'all';
""" if enable_type_filter else "    const selectedType = 'all';\n"
    filter_listener = """
  const typeFilter = document.getElementById('type-filter');
  if (typeFilter) typeFilter.addEventListener('change', applyView);
""" if enable_type_filter else ""
    return f"""
  <script>
    const list = document.getElementById('item-list');
    const sortSelect = document.getElementById('sort-select');
    const searchInput = document.getElementById('search-input');

    function sortItems(items, mode) {{
      items.sort((a, b) => {{
        const aVotes = Number(a.dataset.upvotes || '0');
        const bVotes = Number(b.dataset.upvotes || '0');
        const aCreated = Number(a.dataset.created || '0');
        const bCreated = Number(b.dataset.created || '0');
        const aIndex = Number(a.dataset.index || '0');
        const bIndex = Number(b.dataset.index || '0');
        if (mode === 'upvotes-desc') return bVotes - aVotes || aIndex - bIndex;
        if (mode === 'upvotes-asc') return aVotes - bVotes || aIndex - bIndex;
        if (mode === 'time-asc') return aCreated - bCreated || aIndex - bIndex;
        return bCreated - aCreated || aIndex - bIndex;
      }});
      return items;
    }}

    function applyView() {{
      if (!list || !sortSelect) return;
{filter_block}
      const keyword = (searchInput ? searchInput.value : '').trim().toLowerCase();
      const mode = sortSelect.value;
      const items = Array.from(list.querySelectorAll('.item-card'));
      const ordered = sortItems(items, mode);
      ordered.forEach((item) => {{
        const itemType = item.dataset.type || 'answer';
        const searchText = (item.dataset.search || '').toLowerCase();
        const matchesType = selectedType === 'all' || selectedType === itemType;
        const matchesKeyword = !keyword || searchText.includes(keyword);
        const visible = matchesType && matchesKeyword;
        item.style.display = visible ? '' : 'none';
        list.appendChild(item);
      }});
    }}

    if (sortSelect) sortSelect.addEventListener('change', applyView);
    if (searchInput) searchInput.addEventListener('input', applyView);
{filter_listener}
    applyView();

    // 恢复被懒加载机制隐藏的图片
    document.querySelectorAll('img.lazy').forEach(img => {{
      if (img.dataset.actualsrc) {{
        img.src = img.dataset.actualsrc;
      }} else if (img.dataset.original) {{
        img.src = img.dataset.original;
      }}
    }});
  </script>
"""


def _html_shell(
    title: str,
    hero: str,
    controls: str,
    cards: str,
    enable_type_filter: bool = False,
    web_base: str = "",
) -> str:
    base_script = ""
    if web_base:
        base_script = f"""
  <script>
    if (window.location.pathname === '/file') {{
      const base = document.createElement('base');
      base.href = {web_base!r};
      document.head.appendChild(base);
    }}
  </script>
"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{escape(title)}</title>
{base_script}
  <style>
    :root {{
      --zh-bg: #f6f6f6;
      --zh-card: #ffffff;
      --zh-line: #ebebeb;
      --zh-text: #121212;
      --zh-sub: #646464;
      --zh-blue: #056de8;
      --zh-blue-soft: rgba(5, 109, 232, 0.08);
      --zh-shadow: 0 1px 3px rgba(18, 18, 18, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Helvetica Neue", Arial, sans-serif;
      background: var(--zh-bg);
      color: var(--zh-text);
    }}
    a {{
      color: var(--zh-blue);
      text-decoration: none;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    .topbar {{
      background: rgba(255, 255, 255, 0.95);
      border-bottom: 1px solid var(--zh-line);
      position: sticky;
      top: 0;
      z-index: 20;
      backdrop-filter: blur(12px);
    }}
    .topbar-inner {{
      max-width: 1080px;
      margin: 0 auto;
      padding: 14px 20px;
      font-size: 15px;
      font-weight: 700;
      color: var(--zh-blue);
    }}
    .page {{
      max-width: 1080px;
      margin: 0 auto;
      padding: 24px 20px 48px;
    }}
    .hero-card, .toolbar, .item-card {{
      background: var(--zh-card);
      border: 1px solid var(--zh-line);
      border-radius: 16px;
      box-shadow: var(--zh-shadow);
    }}
    .hero-card {{
      padding: 24px;
      margin-bottom: 16px;
    }}
    .hero-head {{
      display: flex;
      align-items: center;
      gap: 16px;
      margin-bottom: 14px;
    }}
    .hero-avatar {{
      width: 68px;
      height: 68px;
      border-radius: 50%;
      overflow: hidden;
      flex-shrink: 0;
      background: linear-gradient(135deg, #d9e9ff, #edf5ff);
      border: 1px solid rgba(5, 109, 232, 0.12);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 28px;
      color: var(--zh-blue);
      font-weight: 700;
    }}
    .hero-avatar img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}
    .hero-title {{
      font-size: clamp(24px, 3vw, 34px);
      line-height: 1.25;
      margin: 0 0 4px;
      font-weight: 700;
    }}
    .hero-sub {{
      color: var(--zh-sub);
      font-size: 14px;
      line-height: 1.7;
    }}
    .meta-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 16px;
    }}
    .meta-list span {{
      font-size: 13px;
      color: var(--zh-sub);
      background: #fafafa;
      border: 1px solid var(--zh-line);
      border-radius: 999px;
      padding: 8px 12px;
    }}
    .toolbar {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
      padding: 16px 18px;
      margin-bottom: 16px;
    }}
    .toolbar .label {{
      color: var(--zh-sub);
      font-size: 13px;
      font-weight: 600;
    }}
    .toolbar select {{
      appearance: none;
      border: 1px solid var(--zh-line);
      background: #fff;
      color: var(--zh-text);
      border-radius: 10px;
      padding: 10px 14px;
      font-size: 14px;
      min-width: 180px;
      outline: none;
    }}
    .toolbar input[type="search"] {{
      border: 1px solid var(--zh-line);
      background: #fff;
      color: var(--zh-text);
      border-radius: 10px;
      padding: 10px 14px;
      font-size: 14px;
      min-width: 240px;
      outline: none;
      flex: 1 1 260px;
    }}
    .toolbar input[type="search"]::placeholder {{
      color: #8c8c8c;
    }}
    .list {{
      display: flex;
      flex-direction: column;
      gap: 14px;
    }}
    .item-card {{
      padding: 20px 22px;
    }}
    .item-head {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-start;
      margin-bottom: 14px;
    }}
    .item-author {{
      display: flex;
      gap: 12px;
      min-width: 0;
    }}
    .avatar-frame, .avatar-fallback {{
      width: 42px;
      height: 42px;
      border-radius: 50%;
      overflow: hidden;
      flex-shrink: 0;
      background: linear-gradient(135deg, #d9e9ff, #edf5ff);
      border: 1px solid rgba(5, 109, 232, 0.12);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      color: var(--zh-blue);
    }}
    .avatar-img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}
    .author-meta {{
      min-width: 0;
    }}
    .author-name {{
      font-size: 16px;
      font-weight: 600;
      color: var(--zh-text);
      display: inline-block;
      max-width: 100%;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .author-desc {{
      color: var(--zh-sub);
      font-size: 13px;
      margin-top: 4px;
    }}
    .item-stats {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
    }}
    .item-stats span, .type-pill {{
      font-size: 12px;
      color: var(--zh-sub);
      background: #fafafa;
      border: 1px solid var(--zh-line);
      border-radius: 999px;
      padding: 6px 10px;
    }}
    .type-pill {{
      color: var(--zh-blue);
      background: var(--zh-blue-soft);
      border-color: rgba(5, 109, 232, 0.16);
      font-weight: 600;
    }}
    .action-link {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      font-size: 12px;
      color: var(--zh-blue);
      background: var(--zh-blue-soft);
      border: 1px solid rgba(5, 109, 232, 0.16);
      border-radius: 999px;
      padding: 6px 10px;
      font-weight: 600;
      text-decoration: none;
    }}
    .action-link:hover {{
      text-decoration: none;
      filter: brightness(0.98);
    }}
    .title-link {{
      color: var(--zh-text);
      font-size: 20px;
      font-weight: 700;
      line-height: 1.35;
      display: inline-block;
      margin-bottom: 12px;
    }}
    .title-link:hover {{
      color: var(--zh-blue);
      text-decoration: none;
    }}
    .content {{
      line-height: 1.85;
      font-size: 15px;
      color: #262626;
      overflow-wrap: anywhere;
    }}
    .content p {{
      margin: 0 0 14px;
    }}
    .content img {{
      max-width: 100%;
      height: auto;
      display: block;
      margin: 14px auto;
      border-radius: 12px;
      background: #f3f5f7;
    }}
    .content figure {{
      margin: 18px 0;
    }}
    .content pre {{
      overflow-x: auto;
      background: #f7f8fa;
      border-radius: 10px;
      padding: 12px 14px;
      border: 1px solid var(--zh-line);
    }}
    .content blockquote {{
      margin: 16px 0;
      padding: 14px 16px;
      border-radius: 12px;
      border: 1px solid var(--zh-line);
      background: #f7f8fa;
      color: #444;
    }}
    .content blockquote p {{
      margin: 0 0 10px;
    }}
    .content blockquote p:last-child {{
      margin-bottom: 0;
    }}
    .content .pin-reference {{
      margin-top: 18px;
      padding: 18px 20px;
      border-radius: 16px;
      border: 1px solid #e7ebf0;
      background: linear-gradient(180deg, #fafbfc 0%, #f5f7fa 100%);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.7);
    }}
    .content .pin-reference .pin-reference-kind {{
      display: inline-block;
      margin-bottom: 10px;
      font-size: 12px;
      font-weight: 700;
      color: var(--zh-blue);
      background: var(--zh-blue-soft);
      border-radius: 999px;
      padding: 5px 10px;
    }}
    .content .pin-reference .pin-reference-title {{
      display: block;
      margin-bottom: 10px;
      color: var(--zh-text);
      font-size: 18px;
      font-weight: 700;
      line-height: 1.45;
      text-decoration: none;
    }}
    .content .pin-reference .pin-reference-title:hover {{
      color: var(--zh-blue);
      text-decoration: none;
    }}
    .content .pin-reference .pin-reference-summary {{
      color: #6b7280;
      font-size: 14px;
      line-height: 1.7;
      margin: 0;
    }}
    .text-only {{
      white-space: pre-wrap;
      background: #f7f8fa;
      border-radius: 12px;
      padding: 14px 16px;
      border: 1px solid var(--zh-line);
      color: #444;
    }}
    .empty {{
      color: var(--zh-sub);
      font-style: italic;
    }}
    @media (max-width: 768px) {{
      .page {{
        padding: 18px 12px 36px;
      }}
      .hero-card, .item-card, .toolbar {{
        border-radius: 12px;
      }}
      .item-head {{
        flex-direction: column;
      }}
      .item-stats {{
        justify-content: flex-start;
      }}
      .toolbar {{
        flex-direction: column;
        align-items: stretch;
      }}
      .toolbar select {{
        width: 100%;
      }}
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">知乎本地归档浏览页</div>
  </header>
  <main class="page">
    {hero}
    {controls}
    <section class="list" id="item-list">
      {cards}
    </section>
  </main>
{_page_script(enable_type_filter)}
</body>
</html>
"""


class AssetLocalizer:
    def __init__(
        self,
        page_path: str,
        asset_group: str,
        asset_key: str,
        enabled: bool,
        conservative_mode: bool = False,
        variant: str = "dir",
        total_assets: int = 0,
        progress_callback=None,
    ):
        self.page_path = page_path
        self.page_dir = os.path.dirname(page_path)
        self.enabled = enabled
        self.conservative_mode = conservative_mode
        self.variant = variant
        self.asset_dir = os.path.join(OUTPUT_DIR, "html", "assets", asset_group, _safe_filename(asset_key))
        self.session = requests.Session()
        self.cache: dict[str, str] = {}
        self.total_assets = total_assets
        self.progress_callback = progress_callback
        self.processed_assets = 0
        self.saved_assets = 0
        self.failed_assets = 0
        self._progress_started_at = time.monotonic()
        if self.enabled:
            os.makedirs(self.asset_dir, exist_ok=True)

    def _delay(self) -> None:
        if not self.enabled:
            return
        if self.conservative_mode:
            delay = random.uniform(CONSERVATIVE_ASSET_DOWNLOAD_DELAY_MIN, CONSERVATIVE_ASSET_DOWNLOAD_DELAY_MAX)
        else:
            delay = random.uniform(ASSET_DOWNLOAD_DELAY_MIN, ASSET_DOWNLOAD_DELAY_MAX)
        time.sleep(delay)

    def localize_url(self, url: str, prefix: str) -> str:
        if not self.enabled or not url or not url.startswith(("http://", "https://")):
            return url or ""
        if url in self.cache:
            return self.cache[url]

        ext = _guess_extension(url, "")
        filename = f"{_safe_filename(prefix)}-{hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]}{ext}"
        disk_path = os.path.join(self.asset_dir, filename)
        relative_path = os.path.relpath(disk_path, self.page_dir).replace(os.sep, "/")
        if self.variant == "dir" and os.path.exists(disk_path):
            self.processed_assets += 1
            self.saved_assets += 1
            self._emit_progress()
            self.cache[url] = relative_path
            return relative_path

        self._delay()
        try:
            response = self.session.get(url, headers=ZHIMG_HEADERS, timeout=25)
            if response.status_code != 200:
                self.processed_assets += 1
                self.failed_assets += 1
                self._emit_progress()
                self.cache[url] = url
                return url
        except requests.RequestException:
            self.processed_assets += 1
            self.failed_assets += 1
            self._emit_progress()
            self.cache[url] = url
            return url

        ext = _guess_extension(url, response.headers.get("Content-Type", ""))
        if self.variant == "single":
            mime = (response.headers.get("Content-Type") or "").split(";")[0].strip() or f"image/{ext.lstrip('.')}"
            encoded = base64.b64encode(response.content).decode("ascii")
            data_uri = f"data:{mime};base64,{encoded}"
            self.processed_assets += 1
            self.saved_assets += 1
            self._emit_progress()
            self.cache[url] = data_uri
            return data_uri
        filename = f"{_safe_filename(prefix)}-{hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]}{ext}"
        disk_path = os.path.join(self.asset_dir, filename)
        with open(disk_path, "wb") as f:
            f.write(response.content)
        relative_path = os.path.relpath(disk_path, self.page_dir).replace(os.sep, "/")
        self.processed_assets += 1
        self.saved_assets += 1
        self._emit_progress()
        self.cache[url] = relative_path
        return relative_path

    def localize_html(self, html: str, prefix: str) -> str:
        if not self.enabled or not html:
            return html
        rewritten = html
        replacements = []
        for idx, match in enumerate(IMG_URL_RE.finditer(html), start=1):
            url = match.group("url")
            local = self.localize_url(url, f"{prefix}-{idx}")
            if local != url:
                replacements.append((url, local))
        for url, local in replacements:
            rewritten = rewritten.replace(url, local)
        return _normalize_lazy_image_tags(rewritten)

    def _emit_progress(self) -> None:
        if not self.progress_callback:
            return
        if self.processed_assets == 1 or self.processed_assets % 20 == 0 or self.processed_assets == self.total_assets:
            elapsed = max(time.monotonic() - self._progress_started_at, 0.001)
            rate = self.processed_assets / elapsed
            eta_text = "未知"
            if self.total_assets and rate > 0:
                remaining = max(self.total_assets - self.processed_assets, 0)
                eta_text = datetime.fromtimestamp(time.time() + remaining / rate).strftime("%Y-%m-%d %H:%M:%S")
            self.progress_callback(
                "离线资源下载进度: "
                f"{self.processed_assets}/{self.total_assets or '?'}"
                + f", 成功={self.saved_assets}, 失败={self.failed_assets}, 已用时={_format_duration(elapsed)}, 预计完成={eta_text}"
            )


def _extract_asset_urls(html: str) -> list[str]:
    if not html:
        return []
    urls = []
    seen = set()
    for match in IMG_URL_RE.finditer(html):
        url = match.group("url")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _normalize_lazy_image_tags(html: str) -> str:
    if not html or "<img" not in html.lower():
        return html

    def replace_img(match):
        tag = match.group(0)
        attrs = {}
        for attr_match in IMG_ATTR_RE.finditer(tag):
            attrs[attr_match.group("name").lower()] = attr_match.group("value")

        preferred_src = (
            attrs.get("data-actualsrc")
            or attrs.get("data-original")
            or attrs.get("data-src")
            or attrs.get("src")
            or ""
        )
        current_src = attrs.get("src", "")

        if preferred_src and (not current_src or LAZY_PLACEHOLDER_RE.match(current_src)):
            if "src" in attrs:
                tag = re.sub(
                    r"""(?<![\w-])src=(["']).*?\1""",
                    lambda m: f'src="{preferred_src}"',
                    tag,
                    count=1,
                    flags=re.IGNORECASE,
                )
            else:
                tag = tag[:-1] + f' src="{preferred_src}">'

        srcset = attrs.get("srcset", "")
        if srcset and (LAZY_PLACEHOLDER_RE.match(current_src or "") or LAZY_PLACEHOLDER_RE.match(srcset)):
            if preferred_src:
                tag = re.sub(
                    r"""(?<![\w-])srcset=(["']).*?\1""",
                    lambda m: f'srcset="{preferred_src}"',
                    tag,
                    count=1,
                    flags=re.IGNORECASE,
                )
        return tag

    return IMG_TAG_RE.sub(replace_img, html)


def _estimate_question_assets(question) -> list[str]:
    urls = []
    seen = set()

    def add(url: str) -> None:
        if url and url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            urls.append(url)

    if getattr(question, "content_mode", "full") == "full":
        for url in _extract_asset_urls(question.description):
            add(url)
        for answer in question.answers:
            author = answer.author or {}
            add(getattr(author, "avatar_url", "") or "")
            for url in _extract_asset_urls(answer.content):
                add(url)
    return urls


def _estimate_user_assets(user) -> list[str]:
    urls = []
    seen = set()

    def add(url: str) -> None:
        if url and url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            urls.append(url)

    if getattr(user, "content_mode", "full") == "full":
        add(getattr(user, "avatar_url", "") or "")
        for activity in user.activities:
            for url in _extract_asset_urls(activity.content_html):
                add(url)
    return urls


def render_question_html(question, conservative_mode: bool = False, progress_callback=None, variant: str = "dir") -> str:
    html_dir = os.path.join(OUTPUT_DIR, "html", "questions")
    os.makedirs(html_dir, exist_ok=True)
    stem = question_export_stem(question)
    suffix = "-single" if variant == "single" else ""
    path = os.path.join(html_dir, f"{stem}{suffix}.html")
    asset_urls = _estimate_question_assets(question)
    if progress_callback:
        progress_callback(f"✓ 开始生成问题浏览页: {path}")
        if getattr(question, "content_mode", "full") == "full":
            progress_callback(f"✓ 离线资源总数估算: {len(asset_urls)}")
    localizer = AssetLocalizer(
        page_path=path,
        asset_group="questions",
        asset_key=stem,
        enabled=getattr(question, "content_mode", "full") == "full",
        conservative_mode=conservative_mode,
        variant=variant,
        total_assets=len(asset_urls),
        progress_callback=progress_callback,
    )

    answer_cards = []
    for idx, answer in enumerate(question.answers, start=1):
        author = answer.author or {}
        author_name = getattr(author, "name", "") or "匿名用户"
        author_id = getattr(author, "id", "") or ""
        author_headline = getattr(author, "headline", "") or "未提供签名"
        author_link = f"https://www.zhihu.com/people/{author_id}" if author_id else "https://www.zhihu.com/"
        answer_link = f"https://www.zhihu.com/question/{question.id}/answer/{answer.id}"
        avatar_url = localizer.localize_url(getattr(author, "avatar_url", "") or "", f"answer-{answer.id}-avatar")
        content_html = localizer.localize_html(answer.content, f"answer-{answer.id}")
        search_blob = _search_blob(author_name, author_headline, answer.content_text, answer.excerpt)
        answer_cards.append(
            f"""
        <article class="item-card" data-type="answer" data-index="{idx}" data-upvotes="{answer.upvote_count}" data-created="{_timestamp(answer.created_time)}" data-search="{escape(search_blob)}">
          <div class="item-head">
            <div class="item-author">
              {_avatar_markup(avatar_url, author_name, _initial(author_name))}
              <div class="author-meta">
                <a class="author-name" href="{escape(author_link)}" target="_blank" rel="noreferrer">{escape(author_name)}</a>
                <div class="author-desc">{escape(author_headline)}</div>
              </div>
            </div>
            <div class="item-stats">
              <span>#{idx}</span>
              <span>赞同 {answer.upvote_count}</span>
              <span>评论 {answer.comment_count}</span>
              <span>{_format_time(answer.created_time)}</span>
              <a class="action-link" href="{escape(answer_link)}" target="_blank" rel="noreferrer">打开原回答</a>
            </div>
          </div>
          {_content_block(content_html, answer.content_text, getattr(question, "content_mode", "full"))}
        </article>
        """
        )

    description_html = localizer.localize_html(question.description, "question-detail")
    hero = f"""
    <section class="hero-card">
      <div class="hero-head">
        <div class="hero-avatar">问</div>
        <div>
          <h1 class="hero-title">{escape(question.title)}</h1>
          <div class="hero-sub">按点赞或时间重新排序，导出的页面布局更接近知乎阅读视图。</div>
        </div>
      </div>
      {_content_block(description_html, question.description, getattr(question, "content_mode", "full")) if question.description else ''}
      <div class="meta-list">
        <span><a href="https://www.zhihu.com/question/{escape(question.id)}" target="_blank" rel="noreferrer">打开原始问题</a></span>
        <span>问题 ID: {escape(question.id)}</span>
        <span>回答数: {len(question.answers)} / {question.answer_count}</span>
        <span>关注: {question.follower_count}</span>
        <span>评论: {question.comment_count}</span>
        <span>模式: {_mode_label(getattr(question, 'content_mode', 'full'))}</span>
        <span>生成时间: {escape(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</span>
      </div>
    </section>
    """
    controls = """
    <section class="toolbar">
      <span class="label">排序方式</span>
      <select id="sort-select">
        <option value="time-desc">按时间倒序（最新）</option>
        <option value="time-asc">按时间正序（最早）</option>
        <option value="upvotes-desc">按点赞倒序（最高）</option>
        <option value="upvotes-asc">按点赞正序（最低）</option>
      </select>
      <input id="search-input" type="search" placeholder="搜索作者、签名、正文关键词" />
    </section>
    """
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            _html_shell(
                question.title,
                hero,
                controls,
                "".join(answer_cards) or '<div class="item-card empty">没有抓到回答。</div>',
                web_base="/output/html/questions/" if variant == "dir" else "",
            )
        )
    if progress_callback and getattr(question, "content_mode", "full") == "full":
        progress_callback(f"✓ 离线资源下载完成: 成功={localizer.saved_assets}, 失败={localizer.failed_assets}")
    return path


def _activity_link(activity) -> str:
    if activity.type == "article":
        return f"https://zhuanlan.zhihu.com/p/{activity.id}"
    if activity.type == "pin":
        return f"https://www.zhihu.com/pin/{activity.id}"
    if activity.target_id:
        return f"https://www.zhihu.com/question/{activity.target_id}/answer/{activity.id}"
    return "https://www.zhihu.com/"


def _type_label(kind: str) -> str:
    return {
        "answer": "回答",
        "article": "文章",
        "pin": "想法",
    }.get(kind, kind)


def _mode_label(kind: str) -> str:
    return {
        "full": "完整内容（离线图片）",
        "text": "纯文字 JSON",
        "fast": "快速预览",
    }.get(kind, kind)


def _search_blob(*parts: str) -> str:
    merged = " ".join((part or "").strip() for part in parts if part)
    merged = re.sub(r"\s+", " ", merged)
    return merged[:4000]


def render_user_html(user, conservative_mode: bool = False, progress_callback=None, variant: str = "dir") -> str:
    html_dir = os.path.join(OUTPUT_DIR, "html", "users")
    os.makedirs(html_dir, exist_ok=True)
    stem = user_export_stem(user)
    suffix = "-single" if variant == "single" else ""
    path = os.path.join(html_dir, f"{stem}{suffix}.html")
    asset_urls = _estimate_user_assets(user)
    if progress_callback:
        progress_callback(f"✓ 开始生成用户浏览页: {path}")
        if getattr(user, "content_mode", "full") == "full":
            progress_callback(f"✓ 离线资源总数估算: {len(asset_urls)}")
    localizer = AssetLocalizer(
        page_path=path,
        asset_group="users",
        asset_key=stem,
        enabled=getattr(user, "content_mode", "full") == "full",
        conservative_mode=conservative_mode,
        variant=variant,
        total_assets=len(asset_urls),
        progress_callback=progress_callback,
    )

    hero_avatar = localizer.localize_url(getattr(user, "avatar_url", "") or "", "user-avatar")
    cards = []
    for idx, activity in enumerate(user.activities, start=1):
        content_html = localizer.localize_html(activity.content_html, f"{activity.type}-{activity.id}")
        activity_link = _activity_link(activity)
        search_blob = _search_blob(user.name, user.headline, activity.title, activity.excerpt, activity.id)
        cards.append(
            f"""
        <article class="item-card" data-type="{escape(activity.type)}" data-index="{idx}" data-upvotes="{activity.upvote_count}" data-created="{_timestamp(activity.created_time)}" data-search="{escape(search_blob)}">
          <div class="item-head">
            <div class="item-author">
              {_avatar_markup(hero_avatar, user.name or user.id, _initial(user.name or user.id))}
              <div class="author-meta">
                <a class="author-name" href="{escape(_activity_link(activity))}" target="_blank" rel="noreferrer">{escape(activity.title or activity.target_id or activity.id)}</a>
                <div class="author-desc">{escape(user.name)} · {escape(_type_label(activity.type))} · {_format_time(activity.created_time)}</div>
              </div>
            </div>
            <div class="item-stats">
              <span class="type-pill">{escape(_type_label(activity.type))}</span>
              <span>赞同 {activity.upvote_count}</span>
              <span>评论 {activity.comment_count}</span>
              <span>ID {escape(activity.id)}</span>
              <a class="action-link" href="{escape(activity_link)}" target="_blank" rel="noreferrer">打开原{escape(_type_label(activity.type))}</a>
            </div>
          </div>
          {_content_block(content_html, activity.excerpt, getattr(user, "content_mode", "full"))}
        </article>
        """
        )

    hero = f"""
    <section class="hero-card">
      <div class="hero-head">
        <div class="hero-avatar">
          {f'<img src="{escape(hero_avatar)}" alt="{escape(user.name or user.id)}" loading="lazy" />' if hero_avatar else escape(_initial(user.name or user.id))}
        </div>
        <div>
          <h1 class="hero-title">{escape(user.name)}</h1>
          <div class="hero-sub">{escape(user.headline or '这个本地归档页支持类型筛选和排序，阅读体验更接近知乎主页。')}</div>
        </div>
      </div>
      <div class="meta-list">
        <span><a href="https://www.zhihu.com/people/{escape(user.id)}" target="_blank" rel="noreferrer">打开知乎主页</a></span>
        <span>用户 ID: {escape(user.id)}</span>
        <span>动态数: {len(user.activities)}</span>
        <span>粉丝: {user.followers_count}</span>
        <span>关注: {user.following_count}</span>
        <span>内容类型: {escape(' / '.join(getattr(user, 'content_types', []) or ['answer']))}</span>
        <span>模式: {_mode_label(getattr(user, 'content_mode', 'full'))}</span>
        <span>生成时间: {escape(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</span>
      </div>
    </section>
    """
    controls = """
    <section class="toolbar">
      <span class="label">内容类型</span>
      <select id="type-filter">
        <option value="all">全部内容</option>
        <option value="answer">只看回答</option>
        <option value="article">只看文章</option>
        <option value="pin">只看想法</option>
      </select>
      <span class="label">排序方式</span>
      <select id="sort-select">
        <option value="time-desc">按时间倒序（最新）</option>
        <option value="time-asc">按时间正序（最早）</option>
        <option value="upvotes-desc">按点赞倒序（最高）</option>
        <option value="upvotes-asc">按点赞正序（最低）</option>
      </select>
      <input id="search-input" type="search" placeholder="搜索标题、摘要、正文关键词" />
    </section>
    """

    with open(path, "w", encoding="utf-8") as f:
        f.write(
            _html_shell(
                user.name,
                hero,
                controls,
                "".join(cards) or '<div class="item-card empty">没有抓到用户动态。</div>',
                enable_type_filter=True,
                web_base="/output/html/users/" if variant == "dir" else "",
            )
        )
    if progress_callback and getattr(user, "content_mode", "full") == "full":
        progress_callback(f"✓ 离线资源下载完成: 成功={localizer.saved_assets}, 失败={localizer.failed_assets}")
    return path
