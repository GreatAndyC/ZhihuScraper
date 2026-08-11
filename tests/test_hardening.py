from pathlib import Path

from gui import _is_within_directory, _output_web_path
from scraper.base import (
    BROWSER_DELAY_MAX,
    BROWSER_DELAY_MIN,
    FAST_BROWSER_DELAY_MAX,
    FAST_BROWSER_DELAY_MIN,
    FAST_REQUEST_DELAY_MAX,
    FAST_REQUEST_DELAY_MIN,
    BaseScraper,
)


def test_fast_scraper_profile_uses_fast_request_and_browser_delays(monkeypatch):
    random_calls = []

    monkeypatch.setattr(
        "scraper.base.random.uniform",
        lambda low, high: random_calls.append((low, high)) or 0,
    )
    monkeypatch.setattr("scraper.base.time.sleep", lambda _delay: None)

    scraper = BaseScraper(cookie="", fast_mode=True)
    scraper._delay()
    scraper._browser_delay()

    assert random_calls == [
        (FAST_REQUEST_DELAY_MIN, FAST_REQUEST_DELAY_MAX),
        (FAST_BROWSER_DELAY_MIN, FAST_BROWSER_DELAY_MAX),
    ]
    assert (BROWSER_DELAY_MIN, BROWSER_DELAY_MAX) not in random_calls


def test_conservative_profile_takes_precedence_over_fast(monkeypatch):
    random_calls = []
    monkeypatch.setattr(
        "scraper.base.random.uniform",
        lambda low, high: random_calls.append((low, high)) or 0,
    )
    monkeypatch.setattr("scraper.base.time.sleep", lambda _delay: None)

    scraper = BaseScraper(cookie="", conservative_mode=True, fast_mode=True)
    scraper._delay()
    scraper._browser_delay()

    assert scraper.fast_mode is False
    assert random_calls[0] != (FAST_REQUEST_DELAY_MIN, FAST_REQUEST_DELAY_MAX)
    assert random_calls[1] != (FAST_BROWSER_DELAY_MIN, FAST_BROWSER_DELAY_MAX)


def test_gui_path_boundary_rejects_sibling_prefix(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    inside = output_dir / "html" / "page.html"
    sibling = tmp_path / "output-other" / "secret.txt"

    assert _is_within_directory(str(output_dir), str(inside)) is True
    assert _is_within_directory(str(output_dir), str(sibling)) is False


def test_output_web_path_uses_output_route_only_for_real_children(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    inside = output_dir / "html" / "page.html"
    outside = tmp_path / "output-other" / "page.html"

    monkeypatch.setattr("gui.OUTPUT_DIR", str(output_dir))

    assert _output_web_path(str(inside)) == "/output/html/page.html"
    assert _output_web_path(str(outside)) == f"/file?path={Path(outside).resolve()}"
