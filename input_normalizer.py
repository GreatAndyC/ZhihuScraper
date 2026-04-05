import re
from urllib.parse import unquote, urlparse


QUESTION_PATTERNS = [
    re.compile(r"/question/(\d+)"),
    re.compile(r"/questions/(\d+)"),
]

USER_PATTERNS = [
    re.compile(r"/people/([^/?#]+)"),
    re.compile(r"/members/([^/?#]+)"),
]


def normalize_question_input(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.isdigit():
        return raw

    for pattern in QUESTION_PATTERNS:
        match = pattern.search(raw)
        if match:
            return match.group(1)

    parsed = _safe_parse(raw)
    path = unquote(parsed.path or "")
    for pattern in QUESTION_PATTERNS:
        match = pattern.search(path)
        if match:
            return match.group(1)
    return raw


def normalize_user_input(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""

    for pattern in USER_PATTERNS:
        match = pattern.search(raw)
        if match:
            return match.group(1)

    parsed = _safe_parse(raw)
    path = unquote(parsed.path or "")
    for pattern in USER_PATTERNS:
        match = pattern.search(path)
        if match:
            return match.group(1)
    return raw.rstrip("/").split("/")[-1] if raw.startswith(("http://", "https://")) else raw


def _safe_parse(value: str):
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        return urlparse(value)
    if "/" in value or "." in value:
        return urlparse("https://" + value.lstrip("/"))
    return urlparse("")
