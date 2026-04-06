import json

from models import Activity, Question, User
from storage import merge_question_batches, prepare_question_batch_dir, save_question, save_question_batch, save_user


def test_save_question_uses_chinese_title_filename(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.OUTPUT_DIR", str(tmp_path))
    question = Question(id="2009611085918013365", title="普通人要 OpenClaw 有什么用？")

    path = save_question(question, question.id)

    assert path.endswith(".json")
    assert "普通人要-OpenClaw-有什么用" in path


def test_save_user_uses_name_filename(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.OUTPUT_DIR", str(tmp_path))
    user = User(id="ming--li", name="桑桑桑")

    path = save_user(user, user.id)

    assert path.endswith(".json")
    assert path.split("/")[-1] == "桑桑桑-ming--li.json"


def test_merge_question_batches_outputs_named_json(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.OUTPUT_DIR", str(tmp_path))
    question_id = "2009611085918013365"
    prepare_question_batch_dir(question_id)
    save_question_batch(
        {
            "question_id": question_id,
            "question": {"id": question_id, "title": "普通人要 OpenClaw 有什么用？"},
            "answers": [{"id": "1", "content_text": "A"}],
            "batch_index": 1,
            "fetched_count": 1,
            "total_count": 2,
            "method": "api",
        }
    )
    save_question_batch(
        {
            "question_id": question_id,
            "question": {"id": question_id, "title": "普通人要 OpenClaw 有什么用？"},
            "answers": [{"id": "2", "content_text": "B"}],
            "batch_index": 2,
            "fetched_count": 2,
            "total_count": 2,
            "method": "api",
        }
    )

    path = merge_question_batches(question_id)

    assert path is not None
    assert "普通人要-OpenClaw-有什么用" in path
    payload = json.loads(open(path, encoding="utf-8").read())
    assert payload["answer_count"] == 2
    assert len(payload["answers"]) == 2
