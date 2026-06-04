from __future__ import annotations

import json
import os

from config import OUTPUT_DIR
from .models import Chunk, NotebookManifest, Source


def get_notebook_dir(question_id: str) -> str:
    return os.path.join(OUTPUT_DIR, "notebooks", question_id)


def _manifest_path(question_id: str) -> str:
    return os.path.join(get_notebook_dir(question_id), "manifest.json")


def _sources_path(question_id: str) -> str:
    return os.path.join(get_notebook_dir(question_id), "sources.json")


def _chunks_path(question_id: str) -> str:
    return os.path.join(get_notebook_dir(question_id), "chunks.jsonl")


def save_notebook(manifest: NotebookManifest, sources: list[Source], chunks: list[Chunk]) -> str:
    folder = get_notebook_dir(manifest.question_id)
    os.makedirs(folder, exist_ok=True)

    with open(_manifest_path(manifest.question_id), "w", encoding="utf-8") as f:
        json.dump(manifest.model_dump(mode="json"), f, ensure_ascii=False, indent=2)

    with open(_sources_path(manifest.question_id), "w", encoding="utf-8") as f:
        json.dump([item.model_dump(mode="json") for item in sources], f, ensure_ascii=False, indent=2)

    with open(_chunks_path(manifest.question_id), "w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk.model_dump(mode="json"), ensure_ascii=False) + "\n")

    return folder


def load_manifest(question_id: str) -> NotebookManifest:
    with open(_manifest_path(question_id), encoding="utf-8") as f:
        return NotebookManifest(**json.load(f))


def load_sources(question_id: str) -> list[Source]:
    with open(_sources_path(question_id), encoding="utf-8") as f:
        payload = json.load(f)
    return [Source(**item) for item in payload]


def load_chunks(question_id: str) -> list[Chunk]:
    items: list[Chunk] = []
    with open(_chunks_path(question_id), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(Chunk(**json.loads(line)))
    return items
