from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime


class Author(BaseModel):
    id: str
    name: str
    headline: Optional[str] = ""
    avatar_url: Optional[str] = ""


class Answer(BaseModel):
    id: str
    author: Optional[Author] = None
    content: str = ""  # HTML
    content_text: str = ""  # 纯文本
    excerpt: str = ""
    upvote_count: int = 0
    comment_count: int = 0
    created_time: Optional[datetime] = None
    updated_time: Optional[datetime] = None
    is_copyable: bool = False


class Question(BaseModel):
    id: str
    title: str
    content_mode: str = "full"
    description: str = ""
    created_time: Optional[datetime] = None
    updated_time: Optional[datetime] = None
    answer_count: int = 0
    comment_count: int = 0
    follower_count: int = 0
    answers: List[Answer] = Field(default_factory=list)


class Activity(BaseModel):
    id: str
    type: str  # "answer", "article", "pin", etc.
    title: str = ""
    created_time: Optional[datetime] = None
    target_id: str = ""
    excerpt: str = ""
    content_html: str = ""  # 完整 HTML，包含图片和格式
    upvote_count: int = 0
    comment_count: int = 0


class User(BaseModel):
    id: str
    name: str
    content_mode: str = "full"
    content_types: List[str] = Field(default_factory=list)
    headline: Optional[str] = ""
    avatar_url: Optional[str] = ""
    followers_count: int = 0
    following_count: int = 0
    answer_count: int = 0
    articles_count: int = 0
    activities: List[Activity] = Field(default_factory=list)
