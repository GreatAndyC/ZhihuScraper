import re
from datetime import datetime
from typing import Iterable


INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
EXTRA_PUNCT_CHARS = re.compile(r"[！？：；，。、“”‘’（）《》【】]")
WHITESPACE_RE = re.compile(r"\s+")


def safe_filename(value: str, fallback: str = "item", max_length: int = 80) -> str:
    text = (value or "").strip()
    if not text:
        text = fallback
    text = INVALID_FILENAME_CHARS.sub("-", text)
    text = EXTRA_PUNCT_CHARS.sub("-", text)
    text = WHITESPACE_RE.sub("-", text)
    text = re.sub(r"(^-+|-+$)", "", text).strip("-. ")
    if not text:
        text = fallback
    return text[:max_length].rstrip("-. ") or fallback


def question_export_stem(question) -> str:
    title = getattr(question, "title", "") or getattr(question, "id", "") or "question"
    qid = getattr(question, "id", "") or "question"
    return safe_filename(f"{title}-{qid}", fallback=qid)


def user_export_stem(user) -> str:
    name = getattr(user, "name", "") or getattr(user, "id", "") or "user"
    uid = getattr(user, "id", "") or "user"
    return safe_filename(f"{name}-{uid}", fallback=uid)


def build_question_export_meta(
    question,
    *,
    crawl_profile: str,
    html_variant: str,
    source_input: str,
    output_json: str = "",
    output_html: str = "",
) -> dict:
    return {
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_type": "question",
        "source_input": source_input,
        "question_id": getattr(question, "id", ""),
        "question_title": getattr(question, "title", ""),
        "content_mode": getattr(question, "content_mode", "full"),
        "crawl_profile": crawl_profile,
        "html_variant": html_variant,
        "answer_count_declared": getattr(question, "answer_count", 0),
        "answer_count_fetched": len(getattr(question, "answers", []) or []),
        "output_json": output_json,
        "output_html": output_html,
    }


def build_user_export_meta(
    user,
    *,
    crawl_profile: str,
    html_variant: str,
    source_input: str,
    output_json: str = "",
    output_html: str = "",
) -> dict:
    activities = list(getattr(user, "activities", []) or [])
    preview_titles = [item.title for item in activities if getattr(item, "title", "")][:20]
    return {
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_type": "user",
        "source_input": source_input,
        "user_id": getattr(user, "id", ""),
        "user_name": getattr(user, "name", ""),
        "content_mode": getattr(user, "content_mode", "full"),
        "content_types": list(getattr(user, "content_types", []) or []),
        "crawl_profile": crawl_profile,
        "html_variant": html_variant,
        "activity_count_fetched": len(activities),
        "activity_title_preview": preview_titles,
        "output_json": output_json,
        "output_html": output_html,
    }


def format_clock(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def format_datetime_text(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def estimate_task_seconds(cmd: str, mode: str = "full", content_types: Iterable[str] | None = None, html_variant: str = "dir") -> int:
    content_types = list(content_types or [])
    if cmd in {"hot-list", "recommend"}:
        return 15
    if cmd == "question":
        base = 180 if mode == "fast" else (420 if mode == "text" else 960)
        if html_variant == "single" and mode != "text":
            base += 240
        return base
    if cmd == "user":
        type_factor = max(1, len(content_types))
        base = 210 if mode == "fast" else (360 if mode == "text" else 900)
        total = base + (type_factor - 1) * (35 if mode == "fast" else 90)
        if html_variant == "single" and mode != "text":
            total += 240
        return total
    return 300


def format_duration(seconds: float) -> str:
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
