import logging
import math
import json
import re
import time
from datetime import datetime, timedelta
from html import unescape
from typing import Callable, Optional

from config import QUESTION_BATCH_SIZE, REQUEST_DELAY_MAX, REQUEST_DELAY_MIN
from .base import BaseScraper
from models import Question, Answer, Author

logger = logging.getLogger(__name__)


class QuestionScraper(BaseScraper):
    """爬取问题及全部回答"""

    QUESTION_API = "https://www.zhihu.com/api/v4/questions/{question_id}"
    ANSWERS_API = "https://www.zhihu.com/api/v4/questions/{question_id}/answers"
    API_PAGE_SIZE = 20

    def fetch_all(
        self,
        question_id: str,
        should_stop: Optional[Callable[[], bool]] = None,
        wait_if_paused: Optional[Callable[[], bool]] = None,
        batch_callback: Optional[Callable[[dict], Optional[str]]] = None,
        content_mode: str = "full",
    ) -> Optional[Question]:
        def can_continue() -> bool:
            if should_stop and should_stop():
                return False
            if wait_if_paused and not wait_if_paused():
                return False
            return True

        logger.info(f"开始抓取问题 {question_id}")
        question = self._fetch_question_via_api(question_id, can_continue, batch_callback, content_mode)
        if question is not None and (question.answer_count == 0 or len(question.answers) >= question.answer_count):
            logger.info(
                f"问题 {question_id} API 抓取完成: 标题={question.title!r}, 回答={len(question.answers)}, 声明总数={question.answer_count}"
            )
            return question

        if question is not None:
            logger.warning(
                f"问题 {question_id} API 未抓全: 当前 {len(question.answers)} 条, 声明总数 {question.answer_count} 条"
            )

        if content_mode == "fast":
            logger.warning(f"问题 {question_id} 快速模式尝试浏览器接口补抓，不再启用页面 DOM 兜底")
            question = self._fetch_question_via_playwright_api(
                question_id,
                can_continue,
                batch_callback,
                content_mode,
                existing_question=question,
            )
            return question

        logger.warning(f"问题 {question_id} 切换到 Playwright 浏览器接口抓取")
        question = self._fetch_question_via_playwright_api(
            question_id,
            can_continue,
            batch_callback,
            content_mode,
            existing_question=question,
        )
        if question is not None and (question.answer_count == 0 or len(question.answers) >= question.answer_count):
            logger.info(
                f"问题 {question_id} 浏览器接口抓取完成: 标题={question.title!r}, 回答={len(question.answers)}, 声明总数={question.answer_count}"
            )
            return question

        if question is not None:
            logger.warning(
                f"问题 {question_id} 浏览器接口仍未抓全: 当前 {len(question.answers)} 条, 声明总数 {question.answer_count} 条"
            )

        logger.warning(f"问题 {question_id} 最后回退到 Playwright 页面 DOM 抓取")
        return self._fetch_question_via_playwright(
            question_id,
            can_continue,
            batch_callback,
            content_mode,
            existing_question=question,
        )

    def _fetch_question_via_api(
        self,
        question_id: str,
        can_continue: Callable[[], bool],
        batch_callback: Optional[Callable[[dict], Optional[str]]],
        content_mode: str,
    ) -> Optional[Question]:
        if not can_continue():
            return None

        detail_url = self.QUESTION_API.format(question_id=question_id)
        detail_params = {
            "include": "title,detail,excerpt,answer_count,comment_count,follower_count,created,updated_time",
        }
        logger.info(f"请求问题详情: question_id={question_id}")
        detail_resp = self.get(detail_url, params=detail_params, timeout=30)
        if detail_resp.status_code != 200:
            logger.warning(f"问题详情请求失败: status={detail_resp.status_code}")
            return None

        detail = detail_resp.json()
        title = (detail.get("title") or "").strip()
        if not title:
            logger.warning("问题详情响应中没有 title")
            return None

        total_hint = int(detail.get("answer_count") or 0)
        question_meta = {
            "id": question_id,
            "title": title,
            "content_mode": content_mode,
            "description": (
                self._html_to_text(detail.get("detail") or detail.get("excerpt") or "")
                if content_mode in {"text", "fast"}
                else (detail.get("detail") or detail.get("excerpt") or "")
            ),
            "created_time": self._parse_timestamp(detail.get("created")),
            "updated_time": self._parse_timestamp(detail.get("updated_time")),
            "answer_count": total_hint,
            "comment_count": int(detail.get("comment_count") or 0),
            "follower_count": int(detail.get("follower_count") or 0),
            "answers": [],
        }
        self._log_question_plan(method="API 分页", title=title, total_answers=total_hint, content_mode=content_mode)

        answers: list[Answer] = []
        pending_batch: list[Answer] = []
        batch_index = 1
        page_index = 0
        seen_ids = set()
        seen_next_urls = set()
        start_time = time.monotonic()
        page_url = self.ANSWERS_API.format(question_id=question_id)
        current_offset = 0
        next_url = page_url
        next_params = self._build_answer_params(offset=current_offset)

        def flush_pending(force: bool = False, method: str = "api") -> None:
            nonlocal batch_index, pending_batch
            if not batch_callback:
                if force:
                    pending_batch = []
                return

            while len(pending_batch) >= QUESTION_BATCH_SIZE or (force and pending_batch):
                if len(pending_batch) >= QUESTION_BATCH_SIZE:
                    batch_answers = pending_batch[:QUESTION_BATCH_SIZE]
                    pending_batch = pending_batch[QUESTION_BATCH_SIZE:]
                else:
                    batch_answers = pending_batch
                    pending_batch = []

                path = batch_callback({
                    "question_id": question_id,
                    "question": question_meta,
                    "answers": [answer.model_dump() for answer in batch_answers],
                    "batch_index": batch_index,
                    "fetched_count": len(answers) - len(pending_batch),
                    "total_count": total_hint,
                    "method": method,
                })
                if path:
                    logger.info(f"✓ 已保存第 {batch_index} 批回答: {len(batch_answers)} 条 -> {path}")
                else:
                    logger.info(f"✓ 已保存第 {batch_index} 批回答: {len(batch_answers)} 条")
                batch_index += 1

        while next_url:
            if not can_continue():
                flush_pending(force=True)
                return None

            page_index += 1
            logger.info(f"请求回答分页: page={page_index}, url={next_url}")
            resp = self.get(next_url, params=next_params, timeout=30)
            if resp.status_code != 200:
                logger.warning(f"回答分页请求失败: page={page_index}, status={resp.status_code}")
                break

            payload = resp.json()
            items = payload.get("data", [])
            if not items:
                logger.info(f"分页返回空数据，停止抓取: page={page_index}")
                break

            new_count = 0
            for item in items:
                answer_id = str(item.get("id", ""))
                if not answer_id or answer_id in seen_ids:
                    continue
                answer = self._parse_answer_from_api_item(item, content_mode)
                seen_ids.add(answer_id)
                answers.append(answer)
                pending_batch.append(answer)
                new_count += 1

            flush_pending()
            self._log_progress(
                fetched_count=len(answers),
                total_count=total_hint,
                start_time=start_time,
                page_index=page_index,
                new_count=new_count,
            )

            paging = payload.get("paging") or {}
            if paging.get("is_end"):
                logger.info("分页标记 is_end=true，结束回答抓取")
                break

            previous_offset = current_offset
            current_offset += len(items)
            next_url = paging.get("next")
            next_params = None
            if next_url:
                if next_url in seen_next_urls:
                    logger.warning("分页 next URL 重复，停止以避免死循环")
                    break
                seen_next_urls.add(next_url)
                continue

            if current_offset <= previous_offset:
                logger.warning("下一页 offset 未前进，停止以避免死循环")
                break
            next_url = page_url
            next_params = self._build_answer_params(offset=current_offset)

        flush_pending(force=True)
        return Question(
            id=question_id,
            title=title,
            content_mode=content_mode,
            description=question_meta["description"],
            created_time=question_meta["created_time"],
            updated_time=question_meta["updated_time"],
            answer_count=max(total_hint, len(answers)),
            comment_count=question_meta["comment_count"],
            follower_count=question_meta["follower_count"],
            answers=answers,
        )

    def _fetch_question_via_playwright(
        self,
        question_id: str,
        can_continue: Callable[[], bool],
        batch_callback: Optional[Callable[[dict], Optional[str]]],
        content_mode: str,
        existing_question: Optional[Question] = None,
    ) -> Optional[Question]:
        from playwright.sync_api import sync_playwright

        cookie_str = self.cookie
        cookies = []
        for part in cookie_str.split(';'):
            part = part.strip()
            if '=' in part and part:
                name, value = part.split('=', 1)
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".zhihu.com",
                    "path": "/",
                })

        existing_answers = list(existing_question.answers) if existing_question else []
        seen_ids = {answer.id for answer in existing_answers if answer.id}
        pending_batch: list[Answer] = []
        batch_index = (len(existing_answers) // QUESTION_BATCH_SIZE) + 1
        total_hint = existing_question.answer_count if existing_question else 0
        start_time = time.monotonic()

        question_meta = {
            "id": question_id,
            "title": existing_question.title if existing_question else "",
            "content_mode": content_mode,
            "description": existing_question.description if existing_question else "",
            "created_time": existing_question.created_time if existing_question else None,
            "updated_time": existing_question.updated_time if existing_question else None,
            "answer_count": total_hint,
            "comment_count": existing_question.comment_count if existing_question else 0,
            "follower_count": existing_question.follower_count if existing_question else 0,
            "answers": [],
        }

        def flush_pending(force: bool = False) -> None:
            nonlocal batch_index, pending_batch
            if not batch_callback:
                if force:
                    pending_batch = []
                return

            while len(pending_batch) >= QUESTION_BATCH_SIZE or (force and pending_batch):
                if len(pending_batch) >= QUESTION_BATCH_SIZE:
                    batch_answers = pending_batch[:QUESTION_BATCH_SIZE]
                    pending_batch = pending_batch[QUESTION_BATCH_SIZE:]
                else:
                    batch_answers = pending_batch
                    pending_batch = []

                path = batch_callback({
                    "question_id": question_id,
                    "question": question_meta,
                    "answers": [answer.model_dump() for answer in batch_answers],
                    "batch_index": batch_index,
                    "fetched_count": len(existing_answers) - len(pending_batch),
                    "total_count": total_hint,
                    "method": "playwright",
                })
                if path:
                    logger.info(f"✓ 已保存第 {batch_index} 批回答: {len(batch_answers)} 条 -> {path}")
                else:
                    logger.info(f"✓ 已保存第 {batch_index} 批回答: {len(batch_answers)} 条")
                batch_index += 1

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
                page.goto(f"https://www.zhihu.com/question/{question_id}", timeout=30000)
                page.wait_for_timeout(3000)
                self._log_page_state(page, question_id)

                if not question_meta["title"]:
                    question_meta["title"] = self._extract_question_title_from_page(page) or ""

                self._log_question_plan(
                    method="Playwright 页面滚动",
                    title=question_meta["title"] or question_id,
                    total_answers=total_hint,
                    content_mode=content_mode,
                )
                logger.info("进入页面兜底抓取流程，开始滚动加载回答")

                prev_count = 0
                stagnant_rounds = 0
                for i in range(600):
                    if not can_continue():
                        flush_pending(force=True)
                        return None
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1200)

                    items = self._locate_answer_items(page)
                    count = items.count()
                    if count != prev_count:
                        logger.info(f"滚动轮次 {i}: 回答节点 {prev_count} -> {count}")
                        prev_count = count
                        stagnant_rounds = 0
                    else:
                        stagnant_rounds += 1
                    if stagnant_rounds >= 8:
                        logger.info("连续多轮无新增回答，停止滚动")
                        break

                if not can_continue():
                    flush_pending(force=True)
                    return None

                items = self._locate_answer_items(page)
                total = items.count()
                if total == 0:
                    logger.warning("页面模式没有匹配到回答节点，可能是页面结构变化、登录失效或触发安全验证")
                logger.info(f"页面模式开始提取回答: total={total}")

                for i in range(total):
                    if not can_continue():
                        flush_pending(force=True)
                        return None
                    item = items.nth(i)
                    answer = self._parse_answer_from_page_item(page, item, index=i, content_mode=content_mode)
                    if answer.id in seen_ids:
                        continue
                    seen_ids.add(answer.id)
                    existing_answers.append(answer)
                    pending_batch.append(answer)
                    flush_pending()
                    self._log_progress(
                        fetched_count=len(existing_answers),
                        total_count=total_hint,
                        start_time=start_time,
                        page_index=i + 1,
                        new_count=1,
                    )

                flush_pending(force=True)
                if not question_meta["title"]:
                    return None

                return Question(
                    id=question_id,
                    title=question_meta["title"],
                    content_mode=content_mode,
                    description=question_meta["description"],
                    created_time=question_meta["created_time"],
                    updated_time=question_meta["updated_time"],
                    answer_count=max(total_hint, len(existing_answers)),
                    comment_count=question_meta["comment_count"],
                    follower_count=question_meta["follower_count"],
                    answers=existing_answers,
                )
            finally:
                page.close()
                browser.close()

    def _fetch_question_via_playwright_api(
        self,
        question_id: str,
        can_continue: Callable[[], bool],
        batch_callback: Optional[Callable[[dict], Optional[str]]],
        content_mode: str,
        existing_question: Optional[Question] = None,
    ) -> Optional[Question]:
        from playwright.sync_api import sync_playwright

        cookie_str = self.cookie
        cookies = []
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part and part:
                name, value = part.split("=", 1)
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".zhihu.com",
                    "path": "/",
                })

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
                page.goto(f"https://www.zhihu.com/question/{question_id}", timeout=30000)
                page.wait_for_timeout(3000)
                self._log_page_state(page, question_id)

                detail = self._browser_fetch_json(
                    page,
                    f"/api/v4/questions/{question_id}",
                    params={
                        "include": "title,detail,excerpt,answer_count,comment_count,follower_count,created,updated_time",
                    },
                )
                if not detail or detail["status"] != 200:
                    logger.warning(
                        "浏览器问题详情请求失败: "
                        + ("无响应" if not detail else f"status={detail['status']}")
                    )
                    detail_data = self._extract_question_meta_from_dom(page, question_id, existing_question)
                else:
                    detail_data = detail["data"]

                title = (detail_data.get("title") or "").strip()
                if not title:
                    logger.warning("浏览器接口兜底没有获取到标题")
                    return None

                total_hint = int(detail_data.get("answer_count") or 0)
                question_meta = {
                    "id": question_id,
                    "title": title,
                    "content_mode": content_mode,
                    "description": (
                        self._html_to_text(detail_data.get("detail") or detail_data.get("excerpt") or "")
                        if content_mode in {"text", "fast"}
                        else (detail_data.get("detail") or detail_data.get("excerpt") or "")
                    ),
                    "created_time": self._parse_timestamp(detail_data.get("created")),
                    "updated_time": self._parse_timestamp(detail_data.get("updated_time")),
                    "answer_count": total_hint,
                    "comment_count": int(detail_data.get("comment_count") or 0),
                    "follower_count": int(detail_data.get("follower_count") or 0),
                    "answers": [],
                }
                self._log_question_plan(method="Playwright 浏览器接口", title=title, total_answers=total_hint, content_mode=content_mode)

                answers = list(existing_question.answers) if existing_question else []
                seen_ids = {answer.id for answer in answers if answer.id}
                pending_batch: list[Answer] = []
                batch_index = (len(answers) // QUESTION_BATCH_SIZE) + 1
                page_index = 0
                current_offset = len(answers)
                start_time = time.monotonic()

                def flush_pending(force: bool = False) -> None:
                    nonlocal batch_index, pending_batch
                    if not batch_callback:
                        if force:
                            pending_batch = []
                        return

                    while len(pending_batch) >= QUESTION_BATCH_SIZE or (force and pending_batch):
                        if len(pending_batch) >= QUESTION_BATCH_SIZE:
                            batch_answers = pending_batch[:QUESTION_BATCH_SIZE]
                            pending_batch = pending_batch[QUESTION_BATCH_SIZE:]
                        else:
                            batch_answers = pending_batch
                            pending_batch = []

                        path = batch_callback({
                            "question_id": question_id,
                            "question": question_meta,
                            "answers": [answer.model_dump() for answer in batch_answers],
                            "batch_index": batch_index,
                            "fetched_count": len(answers) - len(pending_batch),
                            "total_count": total_hint,
                            "method": "playwright-browser-api",
                        })
                        if path:
                            logger.info(f"✓ 已保存第 {batch_index} 批回答: {len(batch_answers)} 条 -> {path}")
                        else:
                            logger.info(f"✓ 已保存第 {batch_index} 批回答: {len(batch_answers)} 条")
                        batch_index += 1

                while True:
                    if not can_continue():
                        flush_pending(force=True)
                        return None

                    page_index += 1
                    logger.info(
                        f"浏览器分页请求: page={page_index}, offset={current_offset}, limit={self.API_PAGE_SIZE}"
                    )
                    result = self._browser_fetch_json(
                        page,
                        f"/api/v4/questions/{question_id}/answers",
                        params=self._build_answer_params(offset=current_offset),
                    )
                    if not result or result["status"] != 200:
                        logger.warning(
                            "浏览器回答分页请求失败: "
                            + ("无响应" if not result else f"status={result['status']}, offset={current_offset}")
                        )
                        break

                    payload = result["data"]
                    items = payload.get("data", [])
                    if not items:
                        logger.info(f"浏览器分页返回空数据，停止抓取: offset={current_offset}")
                        break

                    new_count = 0
                    for item in items:
                        answer_id = str(item.get("id", ""))
                        if not answer_id or answer_id in seen_ids:
                            continue
                        answer = self._parse_answer_from_api_item(item, content_mode)
                        seen_ids.add(answer_id)
                        answers.append(answer)
                        pending_batch.append(answer)
                        new_count += 1

                    flush_pending()
                    self._log_progress(
                        fetched_count=len(answers),
                        total_count=total_hint,
                        start_time=start_time,
                        page_index=page_index,
                        new_count=new_count,
                    )

                    paging = payload.get("paging") or {}
                    if paging.get("is_end"):
                        logger.info("浏览器分页标记 is_end=true，结束回答抓取")
                        break

                    previous_offset = current_offset
                    current_offset += len(items)
                    if current_offset <= previous_offset:
                        logger.warning("浏览器分页 offset 未前进，停止以避免死循环")
                        break

                flush_pending(force=True)
                return Question(
                    id=question_id,
                    title=title,
                    content_mode=content_mode,
                    description=question_meta["description"],
                    created_time=question_meta["created_time"],
                    updated_time=question_meta["updated_time"],
                    answer_count=max(total_hint, len(answers)),
                    comment_count=question_meta["comment_count"],
                    follower_count=question_meta["follower_count"],
                    answers=answers,
                )
            finally:
                page.close()
                browser.close()

    def _build_answer_params(self, offset: int) -> dict:
        return {
            "include": (
                "data[*].author.url_token,headline,avatar_url;"
                "data[*].content,excerpt,voteup_count,comment_count,created_time,updated_time,is_copyable"
            ),
            "limit": self.API_PAGE_SIZE,
            "offset": offset,
            "platform": "desktop",
            "sort_by": "default",
        }

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
    headers: {
      'accept': 'application/json, text/plain, */*'
    }
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

    def _extract_question_meta_from_dom(self, page, question_id: str, existing_question: Optional[Question]) -> dict:
        title = self._extract_question_title_from_page(page)
        if not title:
            title = existing_question.title if existing_question else ""

        answer_count = existing_question.answer_count if existing_question else 0
        try:
            html = page.content()
            match = re.search(r'"answerCount":\s*(\d+)', html) or re.search(r'"answer_count":\s*(\d+)', html)
            if match:
                answer_count = int(match.group(1))
        except Exception:
            pass

        return {
            "id": question_id,
            "title": title,
            "detail": existing_question.description if existing_question else "",
            "answer_count": answer_count,
            "comment_count": existing_question.comment_count if existing_question else 0,
            "follower_count": existing_question.follower_count if existing_question else 0,
            "created": int(existing_question.created_time.timestamp()) if existing_question and existing_question.created_time else None,
            "updated_time": int(existing_question.updated_time.timestamp()) if existing_question and existing_question.updated_time else None,
        }

    def _extract_question_title_from_page(self, page) -> str:
        selectors = [
            "h1.QuestionHeader-title",
            ".QuestionHeader-title",
            "main h1",
            "h1",
        ]
        for selector in selectors:
            try:
                text = page.locator(selector).first.text_content(timeout=2500)
                if text and text.strip():
                    return text.strip()
            except Exception:
                continue
        try:
            title = (page.title() or "").strip()
        except Exception:
            title = ""
        if title:
            title = re.sub(r"\s*-\s*知乎.*$", "", title).strip()
            if title and title != "知乎":
                return title
        try:
            html = page.content()
            match = (
                re.search(r'"title"\s*:\s*"([^"]+)"', html)
                or re.search(r'<title>([^<]+)</title>', html, re.IGNORECASE)
            )
            if match:
                text = unescape(match.group(1)).strip()
                text = re.sub(r"\s*-\s*知乎.*$", "", text).strip()
                if text and text != "知乎":
                    return text
        except Exception:
            pass
        return ""

    def _locate_answer_items(self, page):
        selectors = [
            ".Question-main .AnswerItem",
            ".Question-main .List-item",
            ".Question-main [data-zop-question-answer]",
            ".AnswerItem",
            ".List-item",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() > 0:
                    return locator
            except Exception:
                continue
        return page.locator(".List-item")

    def _log_page_state(self, page, question_id: str) -> None:
        try:
            title = (page.title() or "").strip()
        except Exception:
            title = ""
        try:
            html = page.content()
        except Exception:
            html = ""
        markers = []
        if "安全验证" in html or "verify" in title.lower():
            markers.append("可能触发安全验证")
        if "登录后" in html or "登录即可" in html:
            markers.append("页面可能处于登录受限状态")
        if markers:
            logger.warning(f"问题页面状态提示: question_id={question_id}, {'；'.join(markers)}")

    def _log_question_plan(self, method: str, title: str, total_answers: int, content_mode: str = "full") -> None:
        if method == "API 分页":
            estimated_seconds = self._estimate_api_duration(total_answers, content_mode)
        else:
            estimated_seconds = self._estimate_playwright_duration(total_answers, content_mode)
        eta = datetime.now() + timedelta(seconds=estimated_seconds)
        logger.info(f"✓ 问题标题: {title}")
        logger.info(f"✓ 当前抓取方法: {method}")
        logger.info(f"✓ 目标回答总数: {total_answers if total_answers else '未知'}")
        logger.info(
            f"✓ 预计耗时: {self._format_duration(estimated_seconds)}，预计完成时间: {eta.strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def _log_progress(
        self,
        fetched_count: int,
        total_count: int,
        start_time: float,
        page_index: int,
        new_count: int,
    ) -> None:
        elapsed = max(time.monotonic() - start_time, 0.001)
        percent = (fetched_count / total_count * 100) if total_count else 0
        rate = fetched_count / elapsed
        eta_text = "未知"
        if total_count and rate > 0:
            remaining = max(total_count - fetched_count, 0)
            eta = datetime.now() + timedelta(seconds=remaining / rate)
            eta_text = eta.strftime("%Y-%m-%d %H:%M:%S")
        logger.info(
            f"进度更新: page={page_index}, 本次新增={new_count}, 累计={fetched_count}"
            + (f"/{total_count} ({percent:.1f}%)" if total_count else "")
            + f", 已用时={self._format_duration(elapsed)}, 预计完成={eta_text}"
        )

    def _parse_answer_from_page_item(self, page, item, index: int, content_mode: str) -> Answer:
        try:
            answer_link = item.locator('a[href*="/answer/"]').first.get_attribute("href", timeout=2000)
        except Exception:
            answer_link = ""
        match = re.search(r"/answer/(\d+)", answer_link or "")
        answer_id = match.group(1) if match else f"page-{index}"

        try:
            vote_el = item.locator('[aria-label*="赞同"]').first
            vote_text = vote_el.text_content(timeout=2000)
            vote_count = int(vote_text.replace("赞同", "").replace(",", "").replace("\u200b", "").strip())
        except Exception:
            vote_count = 0

        try:
            expand_btn = item.locator("button").filter(has_text="查看完整内容")
            if expand_btn.count() > 0:
                expand_btn.first.click()
                page.wait_for_timeout(500)
        except Exception:
            pass

        try:
            content_html = item.locator(".RichText").inner_html(timeout=3000)
            content_text = item.locator(".RichText").text_content(timeout=3000)
        except Exception:
            content_html = ""
            content_text = ""

        try:
            author_el = item.locator('[class*="Author"]')
            author_name = author_el.first.text_content(timeout=2000).strip() if author_el.count() > 0 else ""
        except Exception:
            author_name = ""

        return Answer(
            id=answer_id,
            author=Author(id="", name=author_name),
            content=content_html if content_mode == "full" else "",
            content_text=content_text,
            excerpt=content_text[:200] if content_text else "",
            upvote_count=vote_count,
        )

    @staticmethod
    def _estimate_api_duration(total_answers: int, content_mode: str = "full") -> float:
        request_count = 1 + max(1, math.ceil(max(total_answers, 1) / QuestionScraper.API_PAGE_SIZE))
        avg_request_seconds = ((REQUEST_DELAY_MIN + REQUEST_DELAY_MAX) / 2) + 0.35
        if content_mode == "fast":
            avg_request_seconds = max(0.5, avg_request_seconds * 0.6)
        return request_count * avg_request_seconds

    @staticmethod
    def _estimate_playwright_duration(total_answers: int, content_mode: str = "full") -> float:
        factor = 0.45 if content_mode == "fast" else 0.9
        return 20 + (max(total_answers, 50) * factor)

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

    @staticmethod
    def _parse_timestamp(value) -> Optional[datetime]:
        try:
            if value in (None, ""):
                return None
            return datetime.fromtimestamp(int(value))
        except (TypeError, ValueError, OSError):
            return None

    @staticmethod
    def _html_to_text(value: str) -> str:
        if not value:
            return ""
        text = re.sub(r"<[^>]+>", " ", value)
        text = re.sub(r"\s+", " ", unescape(text))
        return text.strip()

    def _parse_answer_from_api_item(self, item: dict, content_mode: str) -> Answer:
        author_data = item.get("author") or {}
        content_html = item.get("content") or ""
        content_text = self._html_to_text(content_html)
        return Answer(
            id=str(item.get("id", "")),
            author=Author(
                id=str(author_data.get("url_token") or author_data.get("id") or ""),
                name=author_data.get("name") or "",
                headline=author_data.get("headline") or "",
                avatar_url=author_data.get("avatar_url") or "",
            ),
            content=content_html if content_mode == "full" else "",
            content_text=content_text,
            excerpt=(item.get("excerpt") or content_text[:200] or "").strip(),
            upvote_count=int(item.get("voteup_count") or 0),
            comment_count=int(item.get("comment_count") or 0),
            created_time=self._parse_timestamp(item.get("created_time")),
            updated_time=self._parse_timestamp(item.get("updated_time")),
            is_copyable=bool(item.get("is_copyable", False)),
        )
