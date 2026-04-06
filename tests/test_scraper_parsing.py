from scraper.question import QuestionScraper
from scraper.user import UserScraper
from models import Activity


def test_parse_answer_from_api_item_keeps_author_and_text():
    scraper = QuestionScraper(cookie="")
    item = {
        "id": "123",
        "author": {
            "url_token": "alice",
            "name": "Alice",
            "headline": "测试签名",
            "avatar_url": "https://example.com/avatar.jpg",
        },
        "content": "<p>你好 <b>知乎</b></p>",
        "excerpt": "你好 知乎",
        "voteup_count": 11,
        "comment_count": 3,
        "created_time": 1710000000,
        "updated_time": 1710000060,
        "is_copyable": True,
    }

    answer = scraper._parse_answer_from_api_item(item, "full")

    assert answer.id == "123"
    assert answer.author.name == "Alice"
    assert answer.content.startswith("<p>")
    assert "你好 知乎" in answer.content_text
    assert answer.upvote_count == 11


def test_question_dom_title_falls_back_to_page_title_when_h1_missing():
    scraper = QuestionScraper(cookie="")

    class FakeLocator:
        def __init__(self, text="", count=0):
            self._text = text
            self._count = count
            self.first = self

        def text_content(self, timeout=None):
            if not self._text:
                raise RuntimeError("missing")
            return self._text

        def count(self):
            return self._count

    class FakePage:
        def locator(self, selector):
            return FakeLocator("", 0)

        def title(self):
            return "古代的科举制度是中国的一项伟大的制度创新吗？ - 知乎"

        def content(self):
            return "<html></html>"

    title = scraper._extract_question_title_from_page(FakePage())
    assert title == "古代的科举制度是中国的一项伟大的制度创新吗？"


def test_question_locate_answer_items_uses_fallback_selector_order():
    scraper = QuestionScraper(cookie="")

    class FakeLocator:
        def __init__(self, name, count):
            self.name = name
            self._count = count

        def count(self):
            return self._count

    class FakePage:
        def locator(self, selector):
            mapping = {
                ".Question-main .AnswerItem": FakeLocator("a", 0),
                ".Question-main .List-item": FakeLocator("b", 0),
                ".Question-main [data-zop-question-answer]": FakeLocator("c", 3),
            }
            return mapping.get(selector, FakeLocator("z", 0))

    locator = scraper._locate_answer_items(FakePage())
    assert locator.count() == 3


def test_parse_user_answer_activity_in_fast_mode_keeps_excerpt_only():
    scraper = UserScraper(cookie="")
    item = {
        "id": "321",
        "question": {"id": "999", "title": "测试问题"},
        "content": "<p>正文内容</p>",
        "excerpt": "正文内容",
        "voteup_count": 5,
        "comment_count": 1,
        "created_time": 1710000000,
    }

    activity = scraper._parse_activity("answer", item, "fast")

    assert activity is not None
    assert activity.type == "answer"
    assert activity.title == "测试问题"
    assert activity.content_html == ""
    assert "正文内容" in activity.excerpt


def test_parse_user_pin_activity_title_from_content():
    scraper = UserScraper(cookie="")
    item = {
        "id": "pin-1",
        "content_html": "<p>这是一个想法正文</p>",
        "like_count": 7,
        "comment_count": 2,
        "created": 1710000000,
    }

    activity = scraper._parse_activity("pin", item, "full")

    assert activity is not None
    assert activity.type == "pin"
    assert "这是一个想法正文" in activity.title
    assert activity.upvote_count == 7


def test_parse_user_pin_activity_keeps_forwarded_link_card():
    scraper = UserScraper(cookie="")
    item = {
        "id": "pin-2",
        "content_html": "<p>转发一下</p>",
        "target": {
            "type": "文章",
            "url": "https://zhuanlan.zhihu.com/p/123456",
            "title": "原始文章标题",
            "excerpt": "这是转发对象的摘要",
        },
        "created": 1710000000,
    }

    activity = scraper._parse_activity("pin", item, "full")

    assert activity is not None
    assert 'https://zhuanlan.zhihu.com/p/123456' in activity.content_html
    assert '原始文章标题' in activity.content_html
    assert '这是转发对象的摘要' in activity.content_html


def test_pin_reference_html_supports_page_reference_payload():
    html = UserScraper._append_pin_reference_html(
        "<div>正文</div>",
        {
            "page_reference": {
                "url": "https://www.zhihu.com/question/123/answer/456",
                "title": "Manus创始人肖宏为何不直接在美国或者新加坡创业？",
                "summary": "1103 赞同 · 199 评论 · 回答",
                "kind": "转发内容",
            }
        },
    )

    assert 'pin-reference' in html
    assert 'pin-reference-title' in html
    assert 'Manus创始人肖宏为何不直接在美国或者新加坡创业？' in html
    assert '1103 赞同 · 199 评论 · 回答' in html


def test_extract_pin_reference_from_page_falls_back_to_html_url():
    scraper = UserScraper(cookie="")

    class FakePage:
        url = "https://www.zhihu.com/pin/2024349506288797414"

        def evaluate(self, script):
            return None

        def content(self):
            return '''
            <html><body>
              <script>
                window.__DATA__ = {"targetUrl":"https:\\/\\/www.zhihu.com\\/question\\/2018114714098508248\\/answer\\/2020954059209803141"};
              </script>
            </body></html>
            '''

    reference = scraper._extract_pin_reference_from_page(FakePage())

    assert reference is not None
    assert reference["url"] == "https://www.zhihu.com/question/2018114714098508248/answer/2020954059209803141"
    assert reference["kind"] == "转发链接"


def test_user_enrichment_switches_to_page_fallback_after_repeated_403(monkeypatch):
    scraper = UserScraper(cookie="")
    activities = [
        Activity(id="a1", type="answer", title="Q1", target_id="q1"),
        Activity(id="a2", type="answer", title="Q2", target_id="q2"),
        Activity(id="a3", type="answer", title="Q3", target_id="q3"),
        Activity(id="a4", type="answer", title="Q4", target_id="q4"),
    ]
    statuses = [403, 403, 403]
    api_calls = []
    page_calls = []

    def fake_browser_fetch_json(page, path, params=None):
        api_calls.append(path)
        status = statuses.pop(0)
        return {"status": status, "data": None, "text": ""}

    def fake_answer_fallback(page, activity):
        page_calls.append(activity.id)
        return Activity(
            id=activity.id,
            type=activity.type,
            title=activity.title,
            target_id=activity.target_id,
            excerpt="fallback",
            content_html=f"<p>{activity.id}</p>",
        )

    monkeypatch.setattr(scraper, "_browser_fetch_json", fake_browser_fetch_json)
    monkeypatch.setattr(scraper, "_fetch_answer_detail_from_page", fake_answer_fallback)

    enriched = scraper._enrich_activities(page=None, activities=activities, can_continue=lambda: True)

    assert len(enriched) == 4
    assert len(api_calls) == 3
    assert page_calls == ["a1", "a2", "a3", "a4"]


def test_user_article_enrichment_switches_to_page_fallback_after_repeated_403_404(monkeypatch):
    scraper = UserScraper(cookie="")
    activities = [
        Activity(id="p1", type="article", title="A1", target_id="p1"),
        Activity(id="p2", type="article", title="A2", target_id="p2"),
        Activity(id="p3", type="article", title="A3", target_id="p3"),
        Activity(id="p4", type="article", title="A4", target_id="p4"),
    ]
    statuses = [403, 404]
    api_calls = []
    page_calls = []

    def fake_browser_fetch_json(page, path, params=None):
        api_calls.append(path)
        status = statuses.pop(0)
        return {"status": status, "data": None, "text": ""}

    def fake_article_fallback(page, activity):
        page_calls.append(activity.id)
        return Activity(
            id=activity.id,
            type=activity.type,
            title=activity.title,
            target_id=activity.target_id,
            excerpt="fallback",
            content_html=f"<p>{activity.id}</p>",
        )

    monkeypatch.setattr(scraper, "_browser_fetch_json", fake_browser_fetch_json)
    monkeypatch.setattr(scraper, "_fetch_article_detail_from_page", fake_article_fallback)

    enriched = scraper._enrich_activities(page=None, activities=activities, can_continue=lambda: True)

    assert len(enriched) == 4
    assert len(api_calls) == 2
    assert page_calls == ["p1", "p2", "p3", "p4"]
