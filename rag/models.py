from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class QueryType(str, Enum):
    RETRIEVAL = "retrieval"
    GLOBAL_ANALYSIS = "global_analysis"


class Source(BaseModel):
    question_id: str
    question_title: str
    answer_id: str
    author_name: str = ""
    author_id: str = ""
    upvote_count: int = 0
    comment_count: int = 0
    created_time: Optional[datetime] = None
    updated_time: Optional[datetime] = None
    excerpt: str = ""
    content_text: str = ""
    source_url: str = ""


class Chunk(BaseModel):
    chunk_id: str
    question_id: str
    question_title: str
    answer_id: str
    author_name: str = ""
    chunk_index: int = 0
    text: str
    source_url: str = ""
    upvote_count: int = 0
    created_time: Optional[datetime] = None


class Citation(BaseModel):
    answer_id: str
    author_name: str = ""
    source_url: str = ""
    quote: str = ""
    reason: str = ""


class NotebookManifest(BaseModel):
    question_id: str
    question_title: str
    source_count: int = 0
    chunk_count: int = 0
    built_at: datetime = Field(default_factory=datetime.now)
    chunk_target_chars: int = 900
    chunk_overlap_chars: int = 120


class QueryPlan(BaseModel):
    query: str
    query_type: QueryType
    question_id: str
    target_chunk_count: int = 8
    analysis_batch_size: int = 18
    notes: list[str] = Field(default_factory=list)
