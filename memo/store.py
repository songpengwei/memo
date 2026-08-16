"""claims.jsonl 回放式存储：append-only 为唯一 source of truth。

写操作只追加行（assert / supersede / forget / touch）；读取时回放
整个文件算出当前视图。3000 条规模下回放是毫秒级。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path


def data_home() -> Path:
    """数据目录，默认 ~/.memo/，可用 MEMO_HOME 覆盖（测试隔离用）。"""
    return Path(os.environ.get("MEMO_HOME", "~/.memo")).expanduser()


def ensure_dirs(home: Path) -> None:
    (home / "episodic").mkdir(parents=True, exist_ok=True)
    (home / "index").mkdir(parents=True, exist_ok=True)


def claims_path(home: Path) -> Path:
    return home / "claims.jsonl"


def audit_path(home: Path) -> Path:
    return home / "audit.jsonl"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    return "mem_" + uuid.uuid4().hex[:6]


def _append_line(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def append(home: Path, record: dict) -> None:
    """追加一条操作行并写审计日志。"""
    ensure_dirs(home)
    _append_line(claims_path(home), record)
    audit(home, record["op"], record)


def audit(home: Path, action: str, detail: dict) -> None:
    ensure_dirs(home)
    _append_line(audit_path(home), {"ts": now_iso(), "action": action, "detail": detail})


def replay(home: Path) -> dict[str, dict]:
    """回放 claims.jsonl，返回 {id: claim}，每个 claim 带 status 字段。

    status: active / superseded / forgotten。supersede 若带 text，
    则物化一条继承旧 claim 字段的后续 claim（谱系不删行）。
    """
    claims: dict[str, dict] = {}
    p = claims_path(home)
    if not p.exists():
        return claims
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        op = rec.get("op")
        if op == "assert":
            rec["status"] = "active"
            claims[rec["id"]] = rec
        elif op == "supersede":
            tgt = claims.get(rec["target"])
            if tgt is None:
                continue
            if rec.get("text"):
                # 继任 claim：继承旧字段，换新 id 与新文本
                succ = {
                    **tgt,
                    "id": rec.get("new_id") or new_id(),
                    "op": "assert",
                    "text": rec["text"],
                    "created": rec["ts"],
                    "hits": 0,
                    "last_accessed": None,
                    "status": "active",
                }
                claims[succ["id"]] = succ
            tgt["status"] = "superseded"
        elif op == "forget":
            tgt = claims.get(rec["target"])
            if tgt is not None:
                tgt["status"] = "forgotten"
        elif op == "restore":
            # 回滚：被软删/取代的 claim 恢复为 active（append-only 的逆操作）
            tgt = claims.get(rec["target"])
            if tgt is not None and tgt["status"] != "active":
                tgt["status"] = "active"
        elif op == "touch":
            # 召回命中：累加 hits、刷新 last_accessed（append-only）
            tgt = claims.get(rec["target"])
            if tgt is not None and tgt["status"] == "active":
                tgt["hits"] = tgt.get("hits", 0) + 1
                tgt["last_accessed"] = rec["ts"]
        elif op == "confidence":
            tgt = claims.get(rec["target"])
            if tgt is not None:
                tgt["confidence"] = rec["to"]
    return claims


def active_claims(home: Path) -> list[dict]:
    return [c for c in replay(home).values() if c["status"] == "active"]


def remember(
    home: Path,
    text: str,
    key: str | None = None,
    kind: str | None = None,
    entity: str | None = None,
    source: str | None = None,
    confidence: str = "low",
) -> dict:
    """写入一条 claim；若同 key 已有活跃 claim，先自动 supersede（幂等更新）。"""
    if key:
        for c in active_claims(home):
            if c.get("key") == key:
                append(home, {"op": "supersede", "target": c["id"], "text": None, "ts": now_iso()})
    claim = {
        "id": new_id(),
        "op": "assert",
        "text": text,
        "key": key,
        "kind": kind,
        "entity": entity,
        "confidence": confidence,
        "source": source,
        "created": now_iso(),
        "hits": 0,
        "last_accessed": None,
    }
    append(home, claim)
    return claim


def correct(home: Path, target: str, text: str) -> dict | None:
    """append supersede 行（带新文本，回放时物化继任 claim）。返回新 claim。"""
    claims = replay(home)
    if target not in claims or claims[target]["status"] != "active":
        return None
    new_claim_id = new_id()
    append(home, {"op": "supersede", "target": target, "text": text,
                  "new_id": new_claim_id, "ts": now_iso()})
    return replay(home)[new_claim_id]


def forget(home: Path, target: str, hard: bool = False) -> bool:
    """软删：append forget 行。--hard 时物理重写文件移除所有相关行。"""
    claims = replay(home)
    if target not in claims or claims[target]["status"] != "active":
        return False
    append(home, {"op": "forget", "target": target, "hard": hard, "ts": now_iso()})
    if hard:
        kept = []
        for line in claims_path(home).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("id") == target or rec.get("target") == target:
                continue  # 物理移除该 claim 及其全部操作行
            kept.append(line)
        claims_path(home).write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        audit(home, "hard-forget-rewrite", {"target": target})
    return True


def restore(home: Path, target: str) -> bool:
    """回滚软删/取代：append restore 行，claim 恢复 active。"""
    claims = replay(home)
    if target not in claims or claims[target]["status"] == "active":
        return False
    append(home, {"op": "restore", "target": target, "ts": now_iso()})
    return True


_CONFIDENCE_ORDER = ("low", "mid", "high")


def touch(home: Path, target: str) -> None:
    """命中/强化一次：累加 hits，并按阈值自动升级 confidence。"""
    from .budget import CONFIDENCE_HIGH_HITS, CONFIDENCE_MID_HITS

    append(home, {"op": "touch", "target": target, "ts": now_iso()})
    c = replay(home).get(target)
    if c is None:
        return
    hits = c.get("hits", 0)
    cur = _CONFIDENCE_ORDER.index(c.get("confidence") or "low")
    want = cur
    if hits >= CONFIDENCE_HIGH_HITS:
        want = 2
    elif hits >= CONFIDENCE_MID_HITS:
        want = max(want, 1)
    if want > cur:
        append(home, {"op": "confidence", "target": target,
                      "to": _CONFIDENCE_ORDER[want], "ts": now_iso()})


# ---------- episodic 层（情景日志，按月分片，append-only） ----------

def episodic_path(home: Path, month: str) -> Path:
    return home / "episodic" / f"{month}.jsonl"


def log_write(home: Path, text: str, topic: str | None = None,
              entity: str | None = None) -> dict:
    """写一条情景日志到当月分片。"""
    ts = now_iso()
    rec = {"id": "ep_" + uuid.uuid4().hex[:6], "ts": ts,
           "text": text, "topic": topic, "entity": entity}
    ensure_dirs(home)
    _append_line(episodic_path(home, ts[:7]), rec)
    audit(home, "log", rec)
    return rec


def _iter_episodic(home: Path):
    """按月序遍历全部情景日志行。"""
    d = home / "episodic"
    if not d.exists():
        return
    for p in sorted(d.glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                yield json.loads(line)


def log_query(home: Path, since: str | None = None, until: str | None = None,
              topic: str | None = None, entity: str | None = None) -> list[dict]:
    """按时间范围 / topic / entity 查情景日志。since/until 为 YYYY-MM-DD。"""
    out = []
    for rec in _iter_episodic(home):
        day = rec["ts"][:10]
        if since and day < since:
            continue
        if until and day > until:
            continue
        if topic and rec.get("topic") != topic:
            continue
        if entity and rec.get("entity") != entity:
            continue
        out.append(rec)
    return out


def deep_search(home: Path, query: str, k: int = 5) -> list[dict]:
    """episodic 层关键词召回：中文字符 bigram 重叠度打分（不引入分词库）。"""
    def bigrams(s: str) -> set[str]:
        s = "".join(s.split())
        return {s[i:i + 2] for i in range(len(s) - 1)} or {s}

    q = bigrams(query)
    scored = []
    for rec in _iter_episodic(home):
        b = bigrams(rec["text"])
        overlap = len(q & b) / (len(q | b) + 1e-12)
        if overlap > 0:
            scored.append((overlap, rec))
    scored.sort(key=lambda x: -x[0])
    return [{"score": round(s, 4), "layer": "episodic", **r} for s, r in scored[:k]]
