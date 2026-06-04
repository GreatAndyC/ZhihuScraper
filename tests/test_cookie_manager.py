from types import SimpleNamespace

from cookie_manager import (
    clear_cookie_header,
    cookie_header_from_iterable,
    normalize_cookie_header,
    save_cookie_header,
)


def test_normalize_cookie_header_deduplicates_and_strips():
    header = " a = 1 ; b=2; a=3 ; invalid "
    assert normalize_cookie_header(header) == "a=1; b=2"


def test_cookie_header_from_iterable_supports_objects_and_dicts():
    cookies = [
        SimpleNamespace(name="z_c0", value="abc"),
        {"name": "d_c0", "value": "def"},
        {"name": "z_c0", "value": "ignored"},
    ]
    assert cookie_header_from_iterable(cookies) == "z_c0=abc; d_c0=def"


def test_save_and_clear_cookie_header(tmp_path):
    env_path = tmp_path / ".env"
    save_cookie_header("z_c0=abc; d_c0=def", env_path=str(env_path))
    content = env_path.read_text(encoding="utf-8")
    assert "ZHIHU_COOKIE=z_c0=abc; d_c0=def" in content

    clear_cookie_header(env_path=str(env_path))
    assert "ZHIHU_COOKIE=" not in env_path.read_text(encoding="utf-8")
