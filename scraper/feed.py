from typing import Optional, List
import logging

from .base import BaseScraper

logger = logging.getLogger(__name__)


class FeedScraper(BaseScraper):
    """爬取知乎热榜和推荐流"""

    def fetch_hot_list(self, limit: int = 20) -> List[dict]:
        """获取热榜"""
        resp = self.get(
            "https://api.zhihu.com/topstory/hot-list",
            params={"limit": limit},
        )
        if resp.status_code != 200:
            logger.warning(f"Failed to fetch hot list: {resp.status_code}")
            return []
        return resp.json().get("data", [])

    def fetch_recommend(self, page: int = 0, per_page: int = 10) -> List[dict]:
        """获取推荐流"""
        resp = self.get(
            "https://api.zhihu.com/topstory/recommend",
            params={"page": page, "per_page": per_page},
        )
        if resp.status_code != 200:
            logger.warning(f"Failed to fetch recommend: {resp.status_code}")
            return []
        return resp.json().get("data", [])

    def parse_feed_item(self, item: dict) -> dict:
        """解析单个 feed 条目"""
        target = item.get("target", {})
        return {
            "id": target.get("id"),
            "type": target.get("type"),
            "title": target.get("title"),
            "url": target.get("url"),
            "answer_count": target.get("answer_count", 0),
            "follower_count": target.get("follower_count", 0),
            "comment_count": target.get("comment_count", 0),
            "excerpt": target.get("excerpt", ""),
            "created": target.get("created"),
        }
