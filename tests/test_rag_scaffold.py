from datetime import datetime

from models import Answer, Author, Question
from rag.chunker import build_notebook, chunk_source
from rag.models import QueryType, Source
from rag.qa import build_analysis_batches, build_query_plan, classify_query_type
from rag.store import load_chunks, load_manifest, load_sources, save_notebook


def test_build_notebook_creates_sources_and_chunks():
    question = Question(
        id="123",
        title="怎么看测试问题",
        answers=[
            Answer(
                id="a1",
                author=Author(id="u1", name="张三"),
                content_text="这是一段很短的回答。",
                excerpt="这是一段很短的回答。",
                upvote_count=12,
            ),
            Answer(
                id="a2",
                author=Author(id="u2", name="李四"),
                content_text="长回答" * 500,
                excerpt="长回答",
                upvote_count=2,
            ),
        ],
    )

    manifest, sources, chunks = build_notebook(question, chunk_target_chars=120, chunk_overlap_chars=20)

    assert manifest.question_id == "123"
    assert len(sources) == 2
    assert len(chunks) >= 3
    assert chunks[0].source_url.endswith("/answer/a1")
    assert any(chunk.answer_id == "a2" and chunk.chunk_index > 0 for chunk in chunks)


def test_chunk_source_preserves_metadata():
    source = Source(
        question_id="123",
        question_title="测试",
        answer_id="a1",
        author_name="张三",
        source_url="https://www.zhihu.com/question/123/answer/a1",
        upvote_count=18,
        created_time=datetime(2024, 1, 1, 12, 0, 0),
        content_text="A" * 220,
    )

    chunks = chunk_source(source, target_chars=100, overlap_chars=10)

    assert len(chunks) >= 2
    assert chunks[0].author_name == "张三"
    assert chunks[0].upvote_count == 18


def test_query_router_distinguishes_global_analysis_and_retrieval():
    assert classify_query_type("整体来看这些回答的政治倾向如何？") == QueryType.GLOBAL_ANALYSIS
    assert classify_query_type("谁提到了土地财政？") == QueryType.RETRIEVAL


def test_build_analysis_batches_prefers_high_upvote_first():
    sources = [
        Source(question_id="1", question_title="T", answer_id="a1", author_name="A", upvote_count=3, content_text="1"),
        Source(question_id="1", question_title="T", answer_id="a2", author_name="B", upvote_count=10, content_text="2"),
        Source(question_id="1", question_title="T", answer_id="a3", author_name="C", upvote_count=7, content_text="3"),
    ]

    batches = build_analysis_batches(sources, batch_size=2)

    assert len(batches) == 2
    assert [item.answer_id for item in batches[0]] == ["a2", "a3"]


def test_save_and_load_notebook_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("rag.store.OUTPUT_DIR", str(tmp_path))
    question = Question(
        id="q-1",
        title="测试 notebook",
        answers=[
            Answer(
                id="ans-1",
                author=Author(id="u1", name="王五"),
                content_text="这里是一段完整的测试回答。",
                excerpt="这里是一段完整的测试回答。",
                upvote_count=5,
            )
        ],
    )
    manifest, sources, chunks = build_notebook(question, chunk_target_chars=120, chunk_overlap_chars=20)

    folder = save_notebook(manifest, sources, chunks)

    assert folder.endswith("/q-1")
    loaded_manifest = load_manifest("q-1")
    loaded_sources = load_sources("q-1")
    loaded_chunks = load_chunks("q-1")
    plan = build_query_plan(question_id="q-1", query="整体来看这些回答的政治倾向如何？", source_count=len(loaded_sources))

    assert loaded_manifest.source_count == 1
    assert loaded_sources[0].author_name == "王五"
    assert loaded_chunks[0].answer_id == "ans-1"
    assert plan.query_type == QueryType.GLOBAL_ANALYSIS
    assert "预计批次数" in plan.notes[0]
