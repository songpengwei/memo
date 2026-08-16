"""sleep 巩固：Ebbinghaus 衰减遗忘 + 近重复合并 + 冲突带/合并簇建议。

分工人机：
- 机械操作（sleep 自动执行）：retention 过低的遗忘、sim≥0.95 的近重复合并
- 判断操作（只报告，agent/人执行）：冲突带 [0.75, 0.95) 的 reconcile、
  同 entity 簇的语义压缩（remember 合并版 + correct 旧条目）
所有自动操作都是软删/谱系保留，无物理删除。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import embed, store
from .budget import (DECAY_TAU, MERGE_THRESHOLD, RETENTION_FLOOR, REVIEW_SIM_LOW)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _days_idle(c: dict, now: datetime) -> int:
    """距上次被激活的天数（从未命中则按创建时间算）。"""
    return (now - _parse(c.get("last_accessed") or c["created"])).days


def retention(c: dict, now: datetime | None = None) -> float:
    """Ebbinghaus 保持度：exp(-未激活天数 / (TAU × (1+hits)))。

    hits 越多稳定性越强——重复激活抵抗遗忘；从不被想起的记忆指数衰减。
    """
    now = now or datetime.now(timezone.utc)
    stability = DECAY_TAU * (1 + c.get("hits", 0))
    return math.exp(-_days_idle(c, now) / stability)


def _forget_candidates(active: list[dict]) -> list[dict]:
    """retention < RETENTION_FLOOR 且 low confidence 的 claim。"""
    now = datetime.now(timezone.utc)
    out = []
    for c in active:
        r = retention(c, now)
        if r < RETENTION_FLOOR and c.get("confidence") == "low":
            out.append({"id": c["id"], "text": c["text"], "retention": round(r, 3),
                        "days_idle": _days_idle(c, now), "hits": c.get("hits", 0)})
    out.sort(key=lambda x: x["retention"])
    return out


def _sim_pairs(home: Path, active: list[dict]) -> list[tuple[dict, dict, float]]:
    """活跃 claims 中余弦相似度 ≥ REVIEW_SIM_LOW 的对，按相似度降序。"""
    idx = embed.load_index(home)
    if idx is None:
        if embed.reindex(home) == 0:
            return []
        idx = embed.load_index(home)
    ids, vecs = idx
    row = {cid: i for i, cid in enumerate(ids)}
    acts = [c for c in active if c["id"] in row]
    if len(acts) < 2:
        return []
    m = vecs[[row[c["id"]] for c in acts]]
    m = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-12)
    sims = m @ m.T
    pairs = []
    for i in range(len(acts)):
        for j in range(i + 1, len(acts)):
            if sims[i, j] >= REVIEW_SIM_LOW:
                pairs.append((acts[i], acts[j], round(float(sims[i, j]), 4)))
    pairs.sort(key=lambda p: -p[2])
    return pairs


def _keeper(a: dict, b: dict) -> tuple[dict, dict]:
    """保留 hits 高者（平手保留创建早者），返回 (keeper, loser)。"""
    if (a.get("hits", 0), a["created"]) >= (b.get("hits", 0), b["created"]):
        return a, b
    return b, a


def _brief(c: dict) -> dict:
    return {"id": c["id"], "text": c["text"]}


def _episodic_candidates(home: Path, active: list[dict], days: int = 2) -> list[dict]:
    """近 N 天的情景日志 × 语义库最近邻：供 agent 判断 promote / merge / discard。"""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    entries = store.log_query(home, since=since)[:20]
    if not entries:
        return []
    idx = embed.load_index(home)
    if idx is None:
        if embed.reindex(home) == 0:
            return [{"ep": e, "best_match": None} for e in entries]
        idx = embed.load_index(home)
    ids, vecs = idx
    row = {cid: i for i, cid in enumerate(ids)}
    acts = [c for c in active if c["id"] in row]
    if not acts:
        return [{"ep": e, "best_match": None} for e in entries]
    m = vecs[[row[c["id"]] for c in acts]]
    m = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-12)
    q = embed.embed_texts([e["text"] for e in entries])
    q = q / (np.linalg.norm(q, axis=1, keepdims=True) + 1e-12)
    sims = q @ m.T
    out = []
    for i, e in enumerate(entries):
        j = int(np.argmax(sims[i]))
        best = acts[j]
        out.append({"ep": e, "best_match": {"id": best["id"], "text": best["text"],
                                            "entity": best.get("entity"),
                                            "score": round(float(sims[i, j]), 4)}})
    return out


def _save_report(home: Path, report: dict) -> str:
    """巩固报告落盘 reports/YYYY-MM-DD.json（同日覆盖）——遗忘必须可审阅。"""
    d = home / "reports"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{report['ts'][:10]}.json"
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def run(home: Path, dry_run: bool) -> dict:
    active = store.active_claims(home)

    # 同 entity ≥3 条的簇：语义压缩建议（带文本，审阅不用再 show）
    by_entity: dict[str, list[dict]] = {}
    for c in active:
        if c.get("entity"):
            by_entity.setdefault(c["entity"], []).append(c)
    suggestions = [
        {"entity": e, "count": len(cs), "claims": [_brief(c) for c in cs]}
        for e, cs in by_entity.items() if len(cs) >= 3
    ]

    forgotten = _forget_candidates(active)

    # 相似对分两带：≥0.95 自动合并；[0.75, 0.95) 冲突带报给 agent 审
    merged, review, retired = [], [], set()
    for a, b, s in _sim_pairs(home, active):
        if s >= MERGE_THRESHOLD:
            keep, lose = _keeper(a, b)
            if keep["id"] in retired or lose["id"] in retired:
                continue
            retired.add(lose["id"])
            merged.append({"kept": keep["id"], "merged": lose["id"], "score": s,
                           "kept_text": keep["text"], "merged_text": lose["text"]})
        else:
            review.append({"a": _brief(a), "b": _brief(b), "score": s})

    report = {"ts": store.now_iso(), "dry_run": dry_run,
              "merge_suggestions": suggestions,
              "forget_candidates": forgotten, "near_duplicates": merged,
              "review_pairs": review,
              "episodic_candidates": _episodic_candidates(home, active)}

    if not dry_run:
        for f in forgotten:
            store.forget(home, f["id"])
        for m in merged:
            store.append(home, {"op": "supersede", "target": m["merged"],
                                "text": None, "ts": store.now_iso()})
        report["applied"] = {"forgotten": len(forgotten), "merged": len(merged)}
        report["report_file"] = _save_report(home, report)
        store.audit(home, "sleep", {**report["applied"],
                                    "report_file": report["report_file"]})
        if forgotten or merged:
            embed.reindex(home)
    return report
