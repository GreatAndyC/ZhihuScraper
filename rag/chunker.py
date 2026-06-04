from __future__ import annotations

import math
import re
from urllib.parse import quote

from models import Question
from .models import Chunk, NotebookManifest, Source

WHITESPACE_RE = re.compile(r"\s+")


def _normalize_text(value: str) -> str:
    text = WHITESPACE_RE.sub(" ", (value or "").strip())
    return text.strip()


def _answer_source_url(question_id: str, answer_id: str) -> str:
    if answer_id:
        return f"https://www.zhihu.com/question/{quote(question_id)}/answer/{quote(answer_id)}"
    return f"https://www.zhihu.com/question/{quote(question_id)}"


def build_sources(question: Question) -> list[Source]:
    sources: list[Source] = []
    for answer in list(question.answers or []):
        text = _normalize_text(answer.content_text or answer.excerpt or "")
        if not text:
            continue
        sources.append(
            Source(
                question_id=question.id,
                question_title=question.title,
                answer_id=answer.id,
                author_name=(answer.author.name if answer.author else "") or "",
                author_id=(answer.author.id if answer.author else "") or "",
                upvote_count=answer.upvote_count,
                comment_count=answer.comment_count,
                created_time=answer.created_time,
                updated_time=answer.updated_time,
                excerpt=_normalize_text(answer.excerpt or text[:180]),
                content_text=text,
                source_url=_answer_source_url(question.id, answer.id),
            )
        )
    return sources


def split_text_into_chunks(text: str, target_chars: int = 900, overlap_chars: int = 120) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []
    if len(normalized) <= target_chars:
        return [normalized]

    chunks: list[str] = []
    start = 0
    text_length = len(normalized)
    step = max(1, target_chars - overlap_chars)

    while start < text_length:
        end = min(text_length, start + target_chars)
        if end < text_length:
            split_at = normalized.rfind(" ", start, end)
            if split_at > start + int(target_chars * 0.6):
                end = split_at
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= text_length:
            break
        start = max(start + 1, end - overlap_chars if overlap_chars else end)
    return chunks


def chunk_source(source: Source, target_chars: int = 900, overlap_chars: int = 120) -> list[Chunk]:
    parts = split_text_into_chunks(source.content_text, target_chars=target_chars, overlap_chars=overlap_chars)
    chunks: list[Chunk] = []
    for index, part in enumerate(parts):
        chunks.append(
            Chunk(
                chunk_id=f"{source.answer_id}:{index}",
                question_id=source.question_id,
                question_title=source.question_title,
                answer_id=source.answer_id,
                author_name=source.author_name,
                chunk_index=index,
                text=part,
                source_url=source.source_url,
                upvote_count=source.upvote_count,
                created_time=source.created_time,
            )
        )
    return chunks


def build_notebook(
    question: Question,
    *,
    chunk_target_chars: int = 900,
    chunk_overlap_chars: int = 120,
) -> tuple[NotebookManifest, list[Source], list[Chunk]]:
    sources = build_sources(question)
    chunks: list[Chunk] = []
    for source in sources:
        chunks.extend(
            chunk_source(
                source,
                target_chars=chunk_target_chars,
                overlap_chars=chunk_overlap_chars,
            )
        )

    manifest = NotebookManifest(
        question_id=question.id,
        question_title=question.title,
        source_count=len(sources),
        chunk_count=len(chunks),
        chunk_target_chars=chunk_target_chars,
        chunk_overlap_chars=chunk_overlap_chars,
    )
    return manifest, sources, chunks


def estimate_analysis_batches(source_count: int, batch_size: int = 18) -> int:
    if source_count <= 0:
        return 0
    return math.ceil(source_count / max(1, batch_size))
