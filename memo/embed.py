"""fastembed 封装与向量索引（index/vecs.npy + index/ids.json）。

模型懒加载：只有真正需要向量时才初始化（避免 query/status 被模型下载拖慢）。
索引是可重建的派生缓存，claims.jsonl 才是 source of truth。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import store

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
VEC_DIM = 512

_model = None


class ModelMismatchError(RuntimeError):
    """索引由不同模型构建，需 reindex。"""


def get_model():
    """懒加载 fastembed 模型（首次运行会下载约 100MB）。"""
    global _model
    if _model is None:
        from fastembed import TextEmbedding

        _model = TextEmbedding(MODEL_NAME)
    return _model


def render(claim: dict) -> str:
    """检索视图：`[kind|entity|YYYY-MM] text`，缺省字段留空。"""
    kind = claim.get("kind") or ""
    entity = claim.get("entity") or ""
    month = (claim.get("created") or "")[:7]
    return f"[{kind}|{entity}|{month}] {claim['text']}"


def embed_texts(texts: list[str]) -> np.ndarray:
    vecs = list(get_model().embed(texts))
    return np.asarray(vecs, dtype=np.float32)


def _vecs_path(home: Path) -> Path:
    return home / "index" / "vecs.npy"


def _ids_path(home: Path) -> Path:
    return home / "index" / "ids.json"


def save_index(home: Path, ids: list[str], vecs: np.ndarray) -> None:
    store.ensure_dirs(home)
    np.save(_vecs_path(home), vecs.astype(np.float32))
    _ids_path(home).write_text(
        json.dumps({"model": MODEL_NAME, "ids": ids}, ensure_ascii=False), encoding="utf-8"
    )


def load_index(home: Path) -> tuple[list[str], np.ndarray] | None:
    """返回 (ids, vecs)；索引不存在返回 None；模型名不匹配抛 ModelMismatchError。"""
    vp, ip = _vecs_path(home), _ids_path(home)
    if not (vp.exists() and ip.exists()):
        return None
    meta = json.loads(ip.read_text(encoding="utf-8"))
    if meta.get("model") != MODEL_NAME:
        raise ModelMismatchError(
            f"索引由 {meta.get('model')} 构建，当前模型为 {MODEL_NAME}，请运行 memo reindex"
        )
    return meta["ids"], np.load(vp)


def reindex(home: Path) -> int:
    """从 claims.jsonl 全量重建索引，返回索引的 claim 数。"""
    claims = store.active_claims(home)
    ids = [c["id"] for c in claims]
    if claims:
        vecs = embed_texts([render(c) for c in claims])
    else:
        vecs = np.zeros((0, VEC_DIM), dtype=np.float32)
    save_index(home, ids, vecs)
    store.audit(home, "reindex", {"count": len(ids), "model": MODEL_NAME})
    return len(ids)


def add_to_index(home: Path, claim: dict) -> None:
    """remember 后的增量索引更新；索引缺失或模型不匹配时全量重建。"""
    try:
        idx = load_index(home)
    except ModelMismatchError:
        reindex(home)
        return
    if idx is None:
        reindex(home)
        return
    ids, vecs = idx
    v = embed_texts([render(claim)])
    vecs = np.vstack([vecs, v]) if len(ids) else v
    ids.append(claim["id"])
    save_index(home, ids, vecs)
