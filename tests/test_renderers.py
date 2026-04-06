import hashlib
from pathlib import Path

from models import Activity, Answer, Author, Question, User
from renderers import AssetLocalizer, render_question_html, render_user_html


def test_render_question_html_text_mode_contains_sort_controls(tmp_path, monkeypatch):
    monkeypatch.setattr("renderers.OUTPUT_DIR", str(tmp_path))
    question = Question(
        id="2009611085918013365",
        title="普通人要 OpenClaw 有什么用？",
        content_mode="text",
        answers=[
            Answer(
                id="1",
                author=Author(id="alice", name="Alice"),
                content_text="第一条回答",
                upvote_count=12,
            )
        ],
    )

    path = render_question_html(question, variant="dir")
    html = Path(path).read_text(encoding="utf-8")

    assert path.endswith(".html")
    assert "sort-select" in html
    assert "第一条回答" in html
    assert "普通人要 OpenClaw 有什么用？" in html
    assert "https://www.zhihu.com/question/2009611085918013365" in html


def test_render_user_html_single_mode_uses_single_suffix_and_type_filter(tmp_path, monkeypatch):
    monkeypatch.setattr("renderers.OUTPUT_DIR", str(tmp_path))

    def fake_localize_url(self, url, prefix):
        return "data:image/png;base64,ZmFrZQ=="

    def fake_localize_html(self, html, prefix):
        return html.replace("https://example.com/a.png", "data:image/png;base64,ZmFrZQ==")

    monkeypatch.setattr(AssetLocalizer, "localize_url", fake_localize_url)
    monkeypatch.setattr(AssetLocalizer, "localize_html", fake_localize_html)

    user = User(
        id="ming--li",
        name="桑桑桑",
        content_mode="full",
        content_types=["answer", "article"],
        avatar_url="https://example.com/avatar.png",
        activities=[
            Activity(
                id="101",
                type="answer",
                title="回答标题",
                excerpt="摘要",
                content_html='<p>正文<img src="https://example.com/a.png" /></p>',
            )
        ],
    )

    path = render_user_html(user, variant="single")
    html = Path(path).read_text(encoding="utf-8")

    assert path.endswith("-single.html")
    assert "type-filter" in html
    assert "data:image/png;base64" in html
    assert "回答标题" in html
    assert "https://www.zhihu.com/people/ming--li" in html


def test_localize_html_promotes_real_image_url_from_lazy_attributes(tmp_path, monkeypatch):
    monkeypatch.setattr("renderers.OUTPUT_DIR", str(tmp_path))

    def fake_localize_url(self, url, prefix):
        if "real.png" in url:
            return "../assets/questions/a-real.png"
        return url

    monkeypatch.setattr(AssetLocalizer, "localize_url", fake_localize_url)

    localizer = AssetLocalizer(
        page_path=str(tmp_path / "page.html"),
        asset_group="questions",
        asset_key="demo",
        enabled=True,
        variant="dir",
    )
    html = '<p><img src="data:image/svg+xml;base64,PHN2Zz4=" data-original="https://example.com/real.png" /></p>'

    localized = localizer.localize_html(html, "answer-1")

    assert 'src="../assets/questions/a-real.png"' in localized
    assert 'data-original="../assets/questions/a-real.png"' in localized


def test_localize_html_ignores_watermark_src_and_prefers_actualsrc(tmp_path, monkeypatch):
    monkeypatch.setattr("renderers.OUTPUT_DIR", str(tmp_path))

    def fake_localize_url(self, url, prefix):
        if "actual.jpg" in url:
            return "../assets/questions/a-actual.jpg"
        if "origin.jpg" in url:
            return "../assets/questions/a-origin.jpg"
        if "watermark.jpg" in url:
            return "../assets/questions/a-watermark.jpg"
        return url

    monkeypatch.setattr(AssetLocalizer, "localize_url", fake_localize_url)

    localizer = AssetLocalizer(
        page_path=str(tmp_path / "page.html"),
        asset_group="questions",
        asset_key="demo",
        enabled=True,
        variant="dir",
    )
    html = (
        '<figure><img src="data:image/svg+xml;utf8,<svg></svg>" '
        'data-default-watermark-src="https://example.com/watermark.jpg" '
        'data-original="https://example.com/origin.jpg" '
        'data-actualsrc="https://example.com/actual.jpg" /></figure>'
    )

    localized = localizer.localize_html(html, "answer-1")

    assert 'src="../assets/questions/a-actual.jpg"' in localized
    assert 'data-actualsrc="../assets/questions/a-actual.jpg"' in localized
    assert 'data-original="../assets/questions/a-origin.jpg"' in localized
    assert 'data-default-watermark-src="https://example.com/watermark.jpg"' in localized


def test_localize_url_reuses_existing_local_asset_without_redownload(tmp_path, monkeypatch):
    monkeypatch.setattr("renderers.OUTPUT_DIR", str(tmp_path))

    page_path = tmp_path / "html" / "questions" / "demo.html"
    page_path.parent.mkdir(parents=True, exist_ok=True)

    localizer = AssetLocalizer(
        page_path=str(page_path),
        asset_group="questions",
        asset_key="demo",
        enabled=True,
        variant="dir",
        total_assets=1,
    )
    existing_url = "https://example.com/avatar.jpg"
    digest = hashlib.sha1(existing_url.encode("utf-8")).hexdigest()[:12]
    existing_path = Path(localizer.asset_dir) / f"answer-1-avatar-{digest}.jpg"
    existing_path.parent.mkdir(parents=True, exist_ok=True)
    existing_path.write_bytes(b"fake-jpeg")

    def fail_get(*args, **kwargs):
        raise AssertionError("should not redownload existing asset")

    monkeypatch.setattr(localizer.session, "get", fail_get)

    localized = localizer.localize_url(existing_url, "answer-1-avatar")

    assert localized == f"../assets/questions/demo/answer-1-avatar-{digest}.jpg"
