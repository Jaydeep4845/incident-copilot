"""Semantic search over the runbooks, with the same bge-small retrieval stack used in DealScope.

Runbooks are split into sections ("## ..."), embedded once, and searched by cosine similarity.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np

RUNBOOK_DIR = Path(__file__).resolve().parents[1] / "runbooks"
MODEL = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def load_sections(directory: Path = RUNBOOK_DIR) -> list[dict]:
    sections = []
    for path in sorted(directory.glob("*.md")):
        if path.name == "README.md":
            continue
        text = path.read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ").strip()
        for block in text.split("\n## ")[1:]:
            head, _, body = block.partition("\n")
            sections.append(
                {
                    "runbook": path.name,
                    "title": title,
                    "section": head.strip(),
                    "text": body.strip(),
                }
            )
    return sections


@lru_cache(maxsize=1)
def _index():
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL, device="cpu")
    sections = load_sections()
    docs = [f"{s['title']}. {s['section']}. {s['text']}" for s in sections]
    return model, sections, model.encode(docs, normalize_embeddings=True)


def search(query: str, k: int = 2) -> list[dict]:
    model, sections, emb = _index()
    q = model.encode(QUERY_PREFIX + query, normalize_embeddings=True)
    order = np.argsort(-(emb @ q))[:k]
    return [
        {
            "runbook": sections[i]["runbook"],
            "title": sections[i]["title"],
            "section": sections[i]["section"],
            "text": sections[i]["text"][:700],
        }
        for i in order
    ]
