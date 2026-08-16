"""语义召回与 context 打包。"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from . import embed, store
from .budget import CONTEXT_DEFAULT_BUDGET

CHARS_PER_TOKEN = 1.5  # 中文按 1 token ≈ 1.5 字符估算


def est_tokens(text: str) -> float:
    return len(text) / CHARS_PER_TOKEN


def _cosine_scores(q: np.ndarray, vecs: np.ndarray) -> np.ndarray:
    q_norm = q / (np.linalg.norm(q) + 1e-12)
    v_norm = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12)
    return v_norm @ q_norm


def _index_or_rebuild(home: Path):
    idx = embed.load_index(home)
    if idx is None:
        if embed.reindex(home) == 0:
            return None
        idx = embed.load_index(home)
    return idx


def find_similar(home: Path, text: str, kind: str | None = None,
                 entity: str | None = None) -> tuple[dict, float] | None:
    """在活跃 claims 中找与文本最相似的一条（remember 去重用）。"""
    idx = _index_or_rebuild(home)
    if idx is None:
        return None
    ids, vecs = idx
    if not ids:
        return None
    active = {c["id"]: c for c in store.active_claims(home)}
    pseudo = {"text": text, "kind": kind, "entity": entity,
              "created": store.now_iso()}
    q = embed.embed_texts([embed.render(pseudo)])[0]
    scores = _cosine_scores(q, vecs)
    best, best_score, best_claim = None, -1.0, None
    for i, cid in enumerate(ids):
        c = active.get(cid)
        if c is not None and scores[i] > best_score:
            best, best_score, best_claim = cid, float(scores[i]), c
    if best_claim is None:
        return None
    return best_claim, round(best_score, 4)


def recall(home: Path, query: str, k: int = 5) -> list[dict]:
    """余弦 top-k 召回；命中后 touch（累加 hits，自动升级 confidence）。"""
    idx = _index_or_rebuild(home)
    if idx is None:
        return []
    ids, vecs = idx
    claims = store.replay(home)
    active = {i: c for i, c in claims.items() if c["status"] == "active"}

    q = embed.embed_texts([query])[0]
    scores = _cosine_scores(q, vecs)

    results = []
    for i in np.argsort(-scores):
        cid = ids[i]
        claim = active.get(cid)
        if claim is None:
            continue  # 索引里可能残留已 supersede/forget 的旧向量
        results.append({"score": round(float(scores[i]), 4),
                        **{k: v for k, v in claim.items() if k != "op"}})
        if len(results) >= k:
            break

    for r in results:
        store.touch(home, r["id"])
    return results


def context_pack(home: Path, task: str, budget: int = CONTEXT_DEFAULT_BUDGET) -> dict:
    """recall top-20 → 按分数降序截断到 token 预算，输出紧凑画像。"""
    hits = recall(home, task, k=20)
    hits.sort(key=lambda r: -r["score"])
    items, used = [], 0.0
    for h in hits:
        t = est_tokens(h["text"])
        if items and used + t > budget:
            break
        items.append(h)
        used += t
    return {"task": task, "budget": budget, "used_tokens": int(used), "items": items}
