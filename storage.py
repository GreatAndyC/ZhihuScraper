import json
import os
from typing import Type, TypeVar, Generic, Optional

from config import OUTPUT_DIR

T = TypeVar("T")


class JSONStorage:
    def save(self, data: Generic, filepath: str) -> None:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    def load(self, filepath: str, cls: Type[T]) -> T:
        with open(filepath, encoding="utf-8") as f:
            return cls(**json.load(f))


def save_question(question, question_id: str) -> str:
    storage = JSONStorage()
    os.makedirs(os.path.join(OUTPUT_DIR, "questions"), exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "questions", f"{question_id}.json")
    storage.save(question.model_dump(), path)
    return path


def save_user(user, user_id: str) -> str:
    storage = JSONStorage()
    os.makedirs(os.path.join(OUTPUT_DIR, "users"), exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "users", f"{user_id}.json")
    storage.save(user.model_dump(), path)
    return path


def get_question_batch_dir(question_id: str) -> str:
    return os.path.join(OUTPUT_DIR, "question_batches", question_id)


def prepare_question_batch_dir(question_id: str) -> str:
    batch_dir = get_question_batch_dir(question_id)
    os.makedirs(batch_dir, exist_ok=True)
    for name in os.listdir(batch_dir):
        if name.startswith("batch-") and name.endswith(".json"):
            os.remove(os.path.join(batch_dir, name))
    return batch_dir


def save_question_batch(
    payload: dict,
) -> str:
    storage = JSONStorage()
    question_id = payload["question_id"]
    batch_index = int(payload["batch_index"])
    batch_dir = get_question_batch_dir(question_id)
    os.makedirs(batch_dir, exist_ok=True)
    path = os.path.join(batch_dir, f"batch-{batch_index:04d}.json")
    storage.save(payload, path)
    return path


def merge_question_batches(question_id: str) -> Optional[str]:
    storage = JSONStorage()
    batch_dir = get_question_batch_dir(question_id)
    if not os.path.isdir(batch_dir):
        return None

    batch_files = sorted(
        os.path.join(batch_dir, name)
        for name in os.listdir(batch_dir)
        if name.startswith("batch-") and name.endswith(".json")
    )
    if not batch_files:
        return None

    question_meta = None
    answers = []
    seen_ids = set()
    total_count = 0

    for path in batch_files:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        question_meta = payload.get("question") or question_meta
        total_count = max(total_count, int(payload.get("total_count") or 0))
        for answer in payload.get("answers", []):
            answer_id = str(answer.get("id", ""))
            if not answer_id or answer_id in seen_ids:
                continue
            seen_ids.add(answer_id)
            answers.append(answer)

    if not question_meta:
        question_meta = {"id": question_id, "title": question_id}

    merged = dict(question_meta)
    merged["id"] = question_id
    merged["answer_count"] = max(total_count, len(answers))
    merged["answers"] = answers

    os.makedirs(os.path.join(OUTPUT_DIR, "questions"), exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "questions", f"{question_id}.json")
    storage.save(merged, path)
    return path
