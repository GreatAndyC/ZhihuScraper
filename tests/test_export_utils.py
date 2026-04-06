from types import SimpleNamespace

from export_utils import (
    build_question_export_meta,
    build_user_export_meta,
    question_export_stem,
    safe_filename,
    user_export_stem,
)


def test_safe_filename_removes_invalid_chars():
    assert safe_filename('普通人要 OpenClaw 有什么用？ <>:"/\\\\|?*') == "普通人要-OpenClaw-有什么用"


def test_question_export_stem_prefers_title_and_id():
    question = SimpleNamespace(id="2009611085918013365", title="普通人要 OpenClaw 有什么用？")
    stem = question_export_stem(question)
    assert stem.startswith("普通人要-OpenClaw-有什么用")
    assert stem.endswith("2009611085918013365")


def test_user_export_stem_prefers_name_and_id():
    user = SimpleNamespace(id="ming--li", name="桑桑桑")
    assert user_export_stem(user) == "桑桑桑-ming--li"


def test_build_question_export_meta_contains_core_fields():
    question = SimpleNamespace(
        id="2009611085918013365",
        title="普通人要 OpenClaw 有什么用？",
        content_mode="full",
        answer_count=1612,
        answers=[1, 2, 3],
    )
    meta = build_question_export_meta(
        question,
        crawl_profile="standard",
        html_variant="dir",
        source_input="https://www.zhihu.com/question/2009611085918013365",
    )
    assert meta["source_type"] == "question"
    assert meta["question_title"] == "普通人要 OpenClaw 有什么用？"
    assert meta["answer_count_fetched"] == 3


def test_build_user_export_meta_contains_preview_titles():
    activities = [
        SimpleNamespace(title="回答标题一"),
        SimpleNamespace(title="文章标题二"),
    ]
    user = SimpleNamespace(
        id="ming--li",
        name="桑桑桑",
        content_mode="full",
        content_types=["answer", "article"],
        activities=activities,
    )
    meta = build_user_export_meta(
        user,
        crawl_profile="conservative",
        html_variant="single",
        source_input="https://www.zhihu.com/people/ming--li",
    )
    assert meta["source_type"] == "user"
    assert meta["activity_count_fetched"] == 2
    assert meta["activity_title_preview"] == ["回答标题一", "文章标题二"]
