"""hot 层画像：warm 层的机械派生物，永远在线的那一份自我叙事。

无 LLM 参与：按 confidence 权重 + hits 排序，填入 token 预算。
agent 可定期 `memo profile --write` 刷新 ~/.memo/profile.md 并注入 system prompt。
"""
from __future__ import annotations

from pathlib import Path

from . import store
from .budget import HOT_TOKEN_BUDGET
from .recall import est_tokens

_CONFIDENCE_WEIGHT = {"high": 3, "mid": 2, "low": 1}


def build_profile(home: Path, budget: int = HOT_TOKEN_BUDGET) -> dict:
    """confidence 权重优先、hits 次之，从高到低填入预算。"""
    active = store.active_claims(home)
    active.sort(key=lambda c: (-_CONFIDENCE_WEIGHT.get(c.get("confidence") or "low", 1),
                               -c.get("hits", 0), c["created"]))
    items, used = [], 0.0
    for c in active:
        t = est_tokens(c["text"])
        if items and used + t > budget:
            break
        items.append({"id": c["id"], "kind": c.get("kind"), "entity": c.get("entity"),
                      "confidence": c.get("confidence"), "hits": c.get("hits", 0),
                      "text": c["text"]})
        used += t
    return {"budget": budget, "used_tokens": int(used), "items": items}


def profile_path(home: Path) -> Path:
    return home / "profile.md"


def write_profile(home: Path, budget: int = HOT_TOKEN_BUDGET) -> dict:
    pack = build_profile(home, budget)
    lines = [f"- [{it.get('kind') or 'fact'}] {it['text']}" for it in pack["items"]]
    profile_path(home).write_text("\n".join(lines) + ("\n" if lines else ""),
                                  encoding="utf-8")
    store.audit(home, "profile", {"count": len(pack["items"]),
                                  "used_tokens": pack["used_tokens"]})
    return pack
