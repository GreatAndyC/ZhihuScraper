from __future__ import annotations

from .chunker import estimate_analysis_batches
from .models import QueryPlan, QueryType, Source

GLOBAL_ANALYSIS_KEYWORDS = [
    "整体",
    "总体",
    "倾向",
    "主要分歧",
    "哪几派",
    "最完整",
    "最聪明",
    "代表性",
    "总体来看",
    "综合来看",
    "政治倾向",
]


def classify_query_type(query: str) -> QueryType:
    text = (query or "").strip().lower()
    for keyword in GLOBAL_ANALYSIS_KEYWORDS:
        if keyword.lower() in text:
            return QueryType.GLOBAL_ANALYSIS
    return QueryType.RETRIEVAL


def build_query_plan(
    *,
    question_id: str,
    query: str,
    source_count: int = 0,
    target_chunk_count: int = 8,
    analysis_batch_size: int = 18,
) -> QueryPlan:
    query_type = classify_query_type(query)
    notes: list[str] = []
    if query_type == QueryType.GLOBAL_ANALYSIS:
        batch_count = estimate_analysis_batches(source_count, batch_size=analysis_batch_size)
        notes.append(f"建议走全局分析路径，预计批次数：{batch_count}")
    else:
        notes.append(f"建议走检索路径，默认召回 chunk 数：{target_chunk_count}")
    return QueryPlan(
        query=query,
        query_type=query_type,
        question_id=question_id,
        target_chunk_count=target_chunk_count,
        analysis_batch_size=analysis_batch_size,
        notes=notes,
    )


def build_analysis_batches(sources: list[Source], batch_size: int = 18) -> list[list[Source]]:
    ordered = sorted(
        list(sources or []),
        key=lambda item: (item.upvote_count, item.created_time.timestamp() if item.created_time else 0),
        reverse=True,
    )
    if not ordered:
        return []
    out: list[list[Source]] = []
    for index in range(0, len(ordered), max(1, batch_size)):
        out.append(ordered[index:index + max(1, batch_size)])
    return out
