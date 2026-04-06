import json
import os
from types import SimpleNamespace
from typing import Type, TypeVar, Generic, Optional

from config import OUTPUT_DIR
from export_utils import question_export_stem, user_export_stem
from models import Question, User

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
    stem = question_export_stem(question)
    path = os.path.join(OUTPUT_DIR, "questions", f"{stem}.json")
    storage.save(question.model_dump(), path)
    return path


def save_user(user, user_id: str) -> str:
    storage = JSONStorage()
    os.makedirs(os.path.join(OUTPUT_DIR, "users"), exist_ok=True)
    stem = user_export_stem(user)
    path = os.path.join(OUTPUT_DIR, "users", f"{stem}.json")
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
    stem = question_export_stem(SimpleNamespace(**merged))
    path = os.path.join(OUTPUT_DIR, "questions", f"{stem}.json")
    storage.save(merged, path)
    return path


def _iter_json_files(folder: str):
    if not os.path.isdir(folder):
        return
    for name in sorted(os.listdir(folder)):
        if name.endswith(".json"):
            yield os.path.join(folder, name)


def find_existing_question_json(question_id: str) -> Optional[str]:
    folder = os.path.join(OUTPUT_DIR, "questions")
    suffix = f"-{question_id}.json"
    legacy_name = f"{question_id}.json"
    for path in _iter_json_files(folder) or []:
        base = os.path.basename(path)
        if base == legacy_name or base.endswith(suffix):
            return path
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            if str(payload.get("id", "")) == question_id:
                return path
            export_meta = payload.get("export_meta") or {}
            if str(export_meta.get("question_id", "")) == question_id:
                return path
        except Exception:
            continue
    return None


def find_existing_user_json(user_id: str) -> Optional[str]:
    folder = os.path.join(OUTPUT_DIR, "users")
    suffix = f"-{user_id}.json"
    legacy_name = f"{user_id}.json"
    for path in _iter_json_files(folder) or []:
        base = os.path.basename(path)
        if base == legacy_name or base.endswith(suffix):
            return path
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            if str(payload.get("id", "")) == user_id:
                return path
            export_meta = payload.get("export_meta") or {}
            if str(export_meta.get("user_id", "")) == user_id:
                return path
        except Exception:
            continue
    return None


def find_existing_html_for_json(json_path: str, kind: str, variant: str = "dir") -> Optional[str]:
    stem = os.path.splitext(os.path.basename(json_path))[0]
    suffix = "-single" if variant == "single" else ""
    html_path = os.path.join(OUTPUT_DIR, "html", kind, f"{stem}{suffix}.html")
    return html_path if os.path.isfile(html_path) else None


def load_question(path: str) -> Question:
    return JSONStorage().load(path, Question)


def load_user(path: str) -> User:
    return JSONStorage().load(path, User)
