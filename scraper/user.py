import json
import logging
import time
from datetime import datetime, timedelta
from html import unescape
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from .base import BaseScraper
from models import User, Activity

logger = logging.getLogger(__name__)


class UserScraper(BaseScraper):
    """爬取用户主页内容"""

    MEMBER_API = "https://www.zhihu.com/api/v4/members/{user_id}"
    CONTENT_PAGE_SIZE = 20
    SUPPORTED_TYPES = ("answer", "article", "pin")

    def fetch_all(
        self,
        user_id: str,
        should_stop: Optional[Callable[[], bool]] = None,
        wait_if_paused: Optional[Callable[[], bool]] = None,
        content_mode: str = "full",
        content_types: Optional[list[str]] = None,
    ) -> Optional[User]:
        from playwright.sync_api import sync_playwright

        def can_continue() -> bool:
            if should_stop and should_stop():
                return False
            if wait_if_paused and not wait_if_paused():
                return False
            return True

        selected_types = [t for t in (content_types or ["answer"]) if t in self.SUPPORTED_TYPES]
        if not selected_types:
            selected_types = ["answer"]

        logger.info(f"开始抓取用户 {user_id}")
        logger.info(f"用户内容类型: {', '.join(selected_types)}")

        cookie_str = self.cookie
        cookies = []
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part and part:
                name, value = part.split("=", 1)
                cookies.append({"name": name.strip(), "value": value.strip(), "domain": ".zhihu.com", "path": "/"})

        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
            )
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            try:
                if not can_continue():
                    return None
                logger.info("开始打开用户主页，获取资料和总数信息")
                self._browser_delay()
                page.goto(f"https://www.zhihu.com/people/{user_id}", timeout=30000)
                page.wait_for_timeout(2500)

                profile = self._fetch_profile(page, user_id)
                if not profile:
                    logger.warning(f"未能获取用户 {user_id} 的资料")
                    return None

                self._log_user_plan(profile, selected_types, content_mode)

                activities = []
                for content_type in selected_types:
                    if not can_continue():
                        return None
                    logger.info(f"开始抓取用户{self._type_label(content_type)}列表")
                    activities.extend(
                        self._fetch_content_type(
                            page=page,
                            user_id=user_id,
                            content_type=content_type,
                            content_mode=content_mode,
                            can_continue=can_continue,
                        )
                    )

                logger.info(f"用户内容列表抓取完成: 共 {len(activities)} 条")
                if content_mode == "full" and activities:
                    self._log_enrichment_plan(activities)
                    activities = self._enrich_activities(
                        page=page,
                        activities=activities,
                        can_continue=can_continue,
                    )

                activities.sort(
                    key=lambda item: item.created_time.timestamp() if item.created_time else 0,
                    reverse=True,
                )

                return User(
                    id=user_id,
                    name=self._stringify(profile.get("name")) or user_id,
                    content_mode=content_mode,
                    content_types=selected_types,
                    headline=self._stringify(profile.get("headline")),
                    avatar_url=self._stringify(profile.get("avatar_url")),
                    followers_count=int(profile.get("follower_count") or 0),
                    following_count=int(profile.get("following_count") or 0),
                    answer_count=int(profile.get("answer_count") or 0),
                    articles_count=int(profile.get("articles_count") or 0),
                    activities=activities,
                )
            finally:
                page.close()
                browser.close()

    def _fetch_profile(self, page, user_id: str) -> Optional[dict]:
        include = (
            "name,headline,avatar_url,follower_count,following_count,"
            "answer_count,articles_count,pins_count"
        )
        result = self._browser_fetch_json(page, f"/api/v4/members/{user_id}", {"include": include})
        if result and result["status"] == 200 and result["data"]:
            logger.info("用户资料获取成功")
            return result["data"]

        logger.warning("用户资料接口失败，尝试从页面提取基础资料")
        profile = {"id": user_id, "name": user_id, "headline": "", "avatar_url": ""}
        try:
            profile["name"] = page.locator(".ProfileHeader-name").first.text_content(timeout=5000).strip()
        except Exception:
            pass
        try:
            profile["headline"] = page.locator(".ProfileHeader-headline").first.text_content(timeout=3000).strip()
        except Exception:
            pass
        try:
            profile["avatar_url"] = page.locator(".Avatar").first.get_attribute("src", timeout=3000) or ""
        except Exception:
            pass
        return profile

    def _fetch_content_type(
        self,
        page,
        user_id: str,
        content_type: str,
        content_mode: str,
        can_continue: Callable[[], bool],
    ) -> list[Activity]:
        endpoint = self._content_endpoint(user_id, content_type)
        include = self._content_include(content_type)
        offset = 0
        page_index = 0
        items_out: list[Activity] = []
        seen_ids = set()
        start_time = time.monotonic()
        total_hint = None
        next_path = endpoint
        next_params = {
            "include": include,
            "limit": self.CONTENT_PAGE_SIZE,
            "offset": offset,
        }

        while next_path:
            if not can_continue():
                return items_out

            page_index += 1
            logger.info(
                f"请求用户{self._type_label(content_type)}分页: page={page_index}, offset={offset}, limit={next_params.get('limit', self.CONTENT_PAGE_SIZE)}"
            )
            result = self._browser_fetch_json(
                page,
                next_path,
                next_params,
            )
            if not result or result["status"] != 200:
                logger.warning(
                    f"用户{self._type_label(content_type)}分页失败: "
                    + ("无响应" if not result else f"status={result['status']}, offset={offset}")
                )
                break

            payload = result["data"] or {}
            data = payload.get("data", [])
            paging = payload.get("paging") or {}
            if total_hint is None:
                total_hint = int(
                    paging.get("totals")
                    or paging.get("total")
                    or payload.get("count")
                    or 0
                )
            if not data:
                logger.info(f"用户{self._type_label(content_type)}分页为空，停止: offset={offset}")
                break

            new_count = 0
            for item in data:
                activity = self._parse_activity(content_type, item, content_mode)
                if not activity or activity.id in seen_ids:
                    continue
                seen_ids.add(activity.id)
                items_out.append(activity)
                new_count += 1

            self._log_type_progress(
                content_type=content_type,
                fetched_count=len(items_out),
                total_count=total_hint or 0,
                start_time=start_time,
                page_index=page_index,
                new_count=new_count,
            )

            if paging.get("is_end"):
                logger.info(f"用户{self._type_label(content_type)}分页 is_end=true，结束")
                break

            previous_offset = offset
            next_path, next_params = self._next_request_from_paging(paging, endpoint, include, offset + len(data))
            offset = int(next_params.get("offset", offset + len(data)))
            if offset <= previous_offset and next_path == endpoint:
                logger.warning(f"用户{self._type_label(content_type)} offset 未前进，停止以避免死循环")
                break

        return items_out

    def _parse_activity(self, content_type: str, item: dict, content_mode: str) -> Optional[Activity]:
        if content_type == "answer":
            question = item.get("question") or {}
            html = self._normalize_html(item.get("content"))
            text = self._html_to_text(html)
            return Activity(
                id=str(item.get("id", "")),
                type="answer",
                title=self._stringify(question.get("title")),
                target_id=str(question.get("id", "")),
                excerpt=text[:500] if text else (self._stringify(item.get("excerpt")) or self._stringify(question.get("title"))),
                content_html=html if content_mode == "full" else "",
                upvote_count=int(item.get("voteup_count") or 0),
                comment_count=int(item.get("comment_count") or 0),
                created_time=self._parse_timestamp(item.get("created_time")),
            )

        if content_type == "article":
            html = self._normalize_html(item.get("content"))
            text = self._html_to_text(html)
            return Activity(
                id=str(item.get("id", "")),
                type="article",
                title=self._stringify(item.get("title")),
                target_id=str(item.get("id", "")),
                excerpt=text[:500] if text else (self._stringify(item.get("excerpt")) or self._stringify(item.get("title"))),
                content_html=html if content_mode == "full" else "",
                upvote_count=int(item.get("voteup_count") or 0),
                comment_count=int(item.get("comment_count") or 0),
                created_time=self._parse_timestamp(item.get("created")),
            )

        if content_type == "pin":
            html = self._normalize_html(item.get("content_html") or item.get("content"))
            text = self._html_to_text(html)
            title = text[:32] if text else f"想法 {item.get('id', '')}"
            return Activity(
                id=str(item.get("id", "")),
                type="pin",
                title=title,
                target_id=str(item.get("id", "")),
                excerpt=text[:500] if text else title,
                content_html=html if content_mode == "full" else "",
                upvote_count=int(item.get("like_count") or item.get("voteup_count") or 0),
                comment_count=int(item.get("comment_count") or 0),
                created_time=self._parse_timestamp(item.get("created")),
            )

        return None

    def _enrich_activities(self, page, activities: list[Activity], can_continue: Callable[[], bool]) -> list[Activity]:
        enriched: list[Activity] = []
        start_time = time.monotonic()
        total = len(activities)
        success_count = 0

        for index, activity in enumerate(activities, start=1):
            if not can_continue():
                return enriched

            if index == 1 or index % 10 == 0:
                logger.info(
                    f"补全正文详情: index={index}/{total}, type={activity.type}, id={activity.id}"
                )
            detail = self._fetch_activity_detail(page, activity)
            if detail:
                activity = detail
            if activity.content_html:
                success_count += 1

            enriched.append(activity)
            if index == 1 or index % 10 == 0 or index == total:
                self._log_enrichment_progress(
                    fetched_count=index,
                    total_count=total,
                    start_time=start_time,
                    activity=activity,
                )

        logger.info(f"✓ 正文补全完成: 成功={success_count}, 失败={total - success_count}")
        return enriched

    def _fetch_activity_detail(self, page, activity: Activity) -> Optional[Activity]:
        if activity.type == "answer":
            result = self._browser_fetch_json(
                page,
                f"/api/v4/answers/{activity.id}",
                {
                    "include": "content,excerpt,voteup_count,comment_count,created_time,updated_time,question.title,question.id",
                },
            )
            if not result or result["status"] != 200 or not result["data"]:
                status = "无响应" if not result else f"status={result['status']}"
                logger.warning(f"回答详情获取失败: id={activity.id}, {status}，尝试页面兜底")
                return self._fetch_answer_detail_from_page(page, activity)
            data = result["data"]
            question = data.get("question") or {}
            html = self._normalize_html(data.get("content"))
            text = self._html_to_text(html)
            return Activity(
                id=activity.id,
                type="answer",
                title=self._stringify(question.get("title")) or activity.title,
                target_id=str(question.get("id", "") or activity.target_id),
                excerpt=text[:500] if text else (self._stringify(data.get("excerpt")) or activity.excerpt),
                content_html=html,
                upvote_count=int(data.get("voteup_count") or activity.upvote_count or 0),
                comment_count=int(data.get("comment_count") or activity.comment_count or 0),
                created_time=self._parse_timestamp(data.get("created_time")) or activity.created_time,
            )

        if activity.type == "article":
            result = self._browser_fetch_json(
                page,
                f"/api/v4/articles/{activity.id}",
                {
                    "include": "title,content,excerpt,voteup_count,comment_count,created,updated",
                },
            )
            if not result or result["status"] != 200 or not result["data"]:
                status = "无响应" if not result else f"status={result['status']}"
                logger.warning(f"文章详情获取失败: id={activity.id}, {status}，尝试页面兜底")
                return self._fetch_article_detail_from_page(page, activity)
            data = result["data"]
            html = self._normalize_html(data.get("content"))
            text = self._html_to_text(html)
            return Activity(
                id=activity.id,
                type="article",
                title=self._stringify(data.get("title")) or activity.title,
                target_id=activity.target_id or activity.id,
                excerpt=text[:500] if text else (self._stringify(data.get("excerpt")) or activity.excerpt),
                content_html=html,
                upvote_count=int(data.get("voteup_count") or activity.upvote_count or 0),
                comment_count=int(data.get("comment_count") or activity.comment_count or 0),
                created_time=self._parse_timestamp(data.get("created")) or activity.created_time,
            )

        if activity.type == "pin":
            result = self._browser_fetch_json(
                page,
                f"/api/v4/pins/{activity.id}",
                {
                    "include": "content,content_html,excerpt,comment_count,like_count,created",
                },
            )
            if not result or result["status"] != 200 or not result["data"]:
                status = "无响应" if not result else f"status={result['status']}"
                logger.warning(f"想法详情获取失败: id={activity.id}, {status}，尝试页面兜底")
                return self._fetch_pin_detail_from_page(page, activity)
            data = result["data"]
            html = self._normalize_html(data.get("content_html") or data.get("content"))
            text = self._html_to_text(html)
            return Activity(
                id=activity.id,
                type="pin",
                title=activity.title or (text[:32] if text else f"想法 {activity.id}"),
                target_id=activity.target_id or activity.id,
                excerpt=text[:500] if text else (data.get("excerpt") or activity.excerpt),
                content_html=html,
                upvote_count=int(data.get("like_count") or activity.upvote_count or 0),
                comment_count=int(data.get("comment_count") or activity.comment_count or 0),
                created_time=self._parse_timestamp(data.get("created")) or activity.created_time,
            )

        return None

    def _fetch_answer_detail_from_page(self, page, activity: Activity) -> Optional[Activity]:
        if not activity.target_id:
            return None
        try:
            self._browser_delay()
            page.goto(f"https://www.zhihu.com/question/{activity.target_id}/answer/{activity.id}", timeout=30000)
            page.wait_for_timeout(1800)
            html = self._extract_rich_text_html(page)
            text = self._html_to_text(html)
            if html:
                logger.info(f"回答页面兜底成功: id={activity.id}")
            return Activity(
                id=activity.id,
                type=activity.type,
                title=activity.title,
                target_id=activity.target_id,
                excerpt=text[:500] if text else activity.excerpt,
                content_html=html or activity.content_html,
                upvote_count=activity.upvote_count,
                comment_count=activity.comment_count,
                created_time=activity.created_time,
            )
        except Exception as exc:
            logger.warning(f"回答页面兜底失败: id={activity.id}, error={exc}")
            return None

    def _fetch_article_detail_from_page(self, page, activity: Activity) -> Optional[Activity]:
        try:
            self._browser_delay()
            page.goto(f"https://zhuanlan.zhihu.com/p/{activity.id}", timeout=30000)
            page.wait_for_timeout(1800)
            html = self._extract_rich_text_html(page)
            text = self._html_to_text(html)
            if html:
                logger.info(f"文章页面兜底成功: id={activity.id}")
            return Activity(
                id=activity.id,
                type=activity.type,
                title=activity.title,
                target_id=activity.target_id,
                excerpt=text[:500] if text else activity.excerpt,
                content_html=html or activity.content_html,
                upvote_count=activity.upvote_count,
                comment_count=activity.comment_count,
                created_time=activity.created_time,
            )
        except Exception as exc:
            logger.warning(f"文章页面兜底失败: id={activity.id}, error={exc}")
            return None

    def _fetch_pin_detail_from_page(self, page, activity: Activity) -> Optional[Activity]:
        try:
            self._browser_delay()
            page.goto(f"https://www.zhihu.com/pin/{activity.id}", timeout=30000)
            page.wait_for_timeout(1800)
            html = self._extract_rich_text_html(page)
            text = self._html_to_text(html)
            if html:
                logger.info(f"想法页面兜底成功: id={activity.id}")
            return Activity(
                id=activity.id,
                type=activity.type,
                title=activity.title,
                target_id=activity.target_id,
                excerpt=text[:500] if text else activity.excerpt,
                content_html=html or activity.content_html,
                upvote_count=activity.upvote_count,
                comment_count=activity.comment_count,
                created_time=activity.created_time,
            )
        except Exception as exc:
            logger.warning(f"想法页面兜底失败: id={activity.id}, error={exc}")
            return None

    def _extract_rich_text_html(self, page) -> str:
        selectors = [
            ".RichText",
            "[itemprop='text']",
            ".Post-RichTextContainer",
            ".ContentItem-RichText",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    return locator.inner_html(timeout=4000) or ""
            except Exception:
                continue
        return ""

    def _log_user_plan(self, profile: dict, content_types: list[str], content_mode: str) -> None:
        counts = {
            "answer": int(profile.get("answer_count") or 0),
            "article": int(profile.get("articles_count") or 0),
            "pin": int(profile.get("pins_count") or 0),
        }
        selected_total = sum(counts.get(kind, 0) for kind in content_types)
        estimated_seconds = max(12, selected_total * (0.2 if content_mode == "text" else 0.9))
        eta = datetime.now() + timedelta(seconds=estimated_seconds)
        logger.info(f"✓ 用户名: {profile.get('name') or profile.get('id')}")
        logger.info(f"✓ 抓取类型: {', '.join(content_types)}")
        logger.info(
            "✓ 类型计数: "
            + ", ".join(f"{self._type_label(kind)}={counts.get(kind, 0)}" for kind in content_types)
        )
        logger.info(
            f"✓ 预计耗时: {self._format_duration(estimated_seconds)}，预计完成时间: {eta.strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def _log_enrichment_plan(self, activities: list[Activity]) -> None:
        by_type = {}
        for activity in activities:
            by_type[activity.type] = by_type.get(activity.type, 0) + 1
        logger.info(
            "✓ 开始补全正文详情: "
            + ", ".join(f"{self._type_label(kind)}={count}" for kind, count in sorted(by_type.items()))
        )

    def _log_type_progress(
        self,
        content_type: str,
        fetched_count: int,
        total_count: int,
        start_time: float,
        page_index: int,
        new_count: int,
    ) -> None:
        elapsed = max(time.monotonic() - start_time, 0.001)
        rate = fetched_count / elapsed
        percent = (fetched_count / total_count * 100) if total_count else 0
        eta_text = "未知"
        if total_count and rate > 0:
            remaining = max(total_count - fetched_count, 0)
            eta = datetime.now() + timedelta(seconds=remaining / rate)
            eta_text = eta.strftime("%Y-%m-%d %H:%M:%S")
        logger.info(
            f"{self._type_label(content_type)}进度: page={page_index}, 本次新增={new_count}, 累计={fetched_count}"
            + (f"/{total_count} ({percent:.1f}%)" if total_count else "")
            + f", 已用时={self._format_duration(elapsed)}, 预计完成={eta_text}"
        )

    def _log_enrichment_progress(
        self,
        fetched_count: int,
        total_count: int,
        start_time: float,
        activity: Activity,
    ) -> None:
        elapsed = max(time.monotonic() - start_time, 0.001)
        rate = fetched_count / elapsed
        percent = (fetched_count / total_count * 100) if total_count else 0
        eta_text = "未知"
        if total_count and rate > 0:
            remaining = max(total_count - fetched_count, 0)
            eta = datetime.now() + timedelta(seconds=remaining / rate)
            eta_text = eta.strftime("%Y-%m-%d %H:%M:%S")
        logger.info(
            f"正文补全进度: {fetched_count}/{total_count} ({percent:.1f}%), 当前={self._type_label(activity.type)}:{activity.id}, "
            f"已用时={self._format_duration(elapsed)}, 预计完成={eta_text}"
        )

    def _browser_fetch_json(self, page, path: str, params: Optional[dict] = None) -> Optional[dict]:
        self._browser_delay()
        script = """
async ({ path, params }) => {
  const url = new URL(path, window.location.origin);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null) {
        url.searchParams.set(key, String(value));
      }
    }
  }
  const response = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
    headers: { accept: 'application/json, text/plain, */*' }
  });
  const text = await response.text();
  return { status: response.status, text };
}
"""
        try:
            result = page.evaluate(script, {"path": path, "params": params or {}})
        except Exception as exc:
            logger.warning(f"浏览器接口调用异常: path={path}, error={exc}")
            return None

        text = result.get("text") or ""
        if result.get("status") != 200:
            return {"status": result.get("status"), "data": None, "text": text}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"浏览器接口返回非 JSON: path={path}")
            return {"status": result.get("status"), "data": None, "text": text}
        return {"status": result.get("status"), "data": data, "text": text}

    def _next_request_from_paging(self, paging: dict, default_path: str, include: str, fallback_offset: int) -> tuple[str, dict]:
        next_url = paging.get("next")
        if not next_url:
            return default_path, {
                "include": include,
                "limit": self.CONTENT_PAGE_SIZE,
                "offset": fallback_offset,
            }

        parsed = urlparse(next_url)
        params = {}
        for key, values in parse_qs(parsed.query).items():
            if not values:
                continue
            value = values[-1]
            if value.isdigit():
                params[key] = int(value)
            else:
                params[key] = value
        params.setdefault("include", include)
        params.setdefault("limit", self.CONTENT_PAGE_SIZE)
        params.setdefault("offset", fallback_offset)
        return parsed.path or default_path, params

    def _content_endpoint(self, user_id: str, content_type: str) -> str:
        mapping = {
            "answer": f"/api/v4/members/{user_id}/answers",
            "article": f"/api/v4/members/{user_id}/articles",
            "pin": f"/api/v4/members/{user_id}/pins",
        }
        return mapping[content_type]

    def _content_include(self, content_type: str) -> str:
        if content_type == "answer":
            return (
                "data[*].question.title,excerpt,content,voteup_count,comment_count,"
                "created_time,updated_time"
            )
        if content_type == "article":
            return "data[*].title,excerpt,content,voteup_count,comment_count,created,updated"
        return "data[*].content,content_html,excerpt,comment_count,like_count,created"

    @staticmethod
    def _type_label(content_type: str) -> str:
        return {
            "answer": "回答",
            "article": "文章",
            "pin": "想法",
        }.get(content_type, content_type)

    @staticmethod
    def _parse_timestamp(value) -> Optional[datetime]:
        try:
            if value in (None, ""):
                return None
            return datetime.fromtimestamp(int(value))
        except (TypeError, ValueError, OSError):
            return None

    @staticmethod
    def _normalize_html(value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = [UserScraper._normalize_html(item) for item in value if item is not None]
            return "".join(parts)
        if isinstance(value, dict):
            if isinstance(value.get("content"), str):
                return value["content"]
            if isinstance(value.get("text"), str):
                return f"<p>{value['text']}</p>"
            return json.dumps(value, ensure_ascii=False)
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _stringify(value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(UserScraper._stringify(item) for item in value if item is not None).strip()
        if isinstance(value, dict):
            if "text" in value:
                return UserScraper._stringify(value.get("text"))
            if "content" in value:
                return UserScraper._stringify(value.get("content"))
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    @staticmethod
    def _html_to_text(value: str) -> str:
        if isinstance(value, list):
            return "\n".join(UserScraper._html_to_text(item) for item in value if item is not None).strip()
        if isinstance(value, dict):
            if "text" in value and isinstance(value["text"], str):
                return value["text"].strip()
            if "content" in value:
                return UserScraper._html_to_text(value["content"])
            return json.dumps(value, ensure_ascii=False)
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        if not value:
            return ""
        text = value.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
        text = unescape(text)
        import re
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
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
