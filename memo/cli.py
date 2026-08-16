"""argparse 子命令分发、退出码、JSONL 输出。

退出码：0 有结果 / 1 错误 / 2 无结果。默认输出 JSONL（一行一条），--pretty 给人看。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import embed, profile as profile_mod, recall as recall_mod, sleep as sleep_mod, store
from .budget import (CONTEXT_DEFAULT_BUDGET, DEDUP_THRESHOLD, HOT_TOKEN_BUDGET,
                     PRESSURE_WARN, WARM_LIMIT, pressure)

# confidence 等级 → 数值，供 --min-confidence 过滤
CONFIDENCE_RANK = {"low": 0.3, "mid": 0.6, "high": 0.9}

EXIT_OK, EXIT_ERR, EXIT_EMPTY = 0, 1, 2


def _emit(record: dict, pretty: bool = False) -> None:
    if pretty:
        print(_pretty(record))
    else:
        print(json.dumps(record, ensure_ascii=False))


def _pretty(rec: dict) -> str:
    """人类可读的一行渲染。"""
    if "score" in rec:
        return f"{rec['score']:.4f}  {rec['id']}  [{rec.get('kind') or ''}|{rec.get('entity') or ''}] {rec['text']}"
    if "text" in rec:
        return f"{rec['id']}  [{rec.get('kind') or ''}|{rec.get('entity') or ''}] ({rec.get('status', 'active')}) {rec['text']}"
    return json.dumps(rec, ensure_ascii=False, indent=2)


def _claim_out(c: dict) -> dict:
    """去掉内部 op 字段的输出视图。"""
    return {k: v for k, v in c.items() if k != "op"}


def _profile_info(home) -> dict:
    p = profile_mod.profile_path(home)
    if not p.exists():
        return {"exists": False}
    lines = p.read_text(encoding="utf-8").splitlines()
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    return {"exists": True, "count": len([l for l in lines if l.strip()]),
            "updated": mtime.strftime("%Y-%m-%dT%H:%M:%SZ")}


# ---------- 各子命令 ----------

def cmd_remember(args) -> int:
    home = store.data_home()
    # 无 key 写入先去重：已有近义 claim 则强化它而非新增（重复听到 = 巩固证据）
    if not args.key:
        sim = recall_mod.find_similar(home, args.text, kind=args.kind, entity=args.entity)
        if sim is not None and sim[1] >= DEDUP_THRESHOLD:
            claim, score = sim
            store.touch(home, claim["id"])
            out = {"merged_into": claim["id"], "score": score,
                   "hits": claim.get("hits", 0) + 1,
                   "pressure": round(pressure(len(store.active_claims(home))), 4)}
            _emit(out, args.pretty)
            return EXIT_OK
    claim = store.remember(home, args.text, key=args.key, kind=args.kind,
                           entity=args.entity, source=args.source, confidence=args.confidence)
    embed.add_to_index(home, claim)
    n = len(store.active_claims(home))
    p = pressure(n)
    out = {"id": claim["id"], "pressure": round(p, 4), "active_claims": n}
    if p >= PRESSURE_WARN:
        out["warning"] = f"pressure {p:.2f} ≥ {PRESSURE_WARN}，建议运行 memo sleep --dry-run 查看降压建议"
    _emit(out, args.pretty)
    return EXIT_OK


def cmd_recall(args) -> int:
    home = store.data_home()
    results = recall_mod.recall(home, args.query, k=args.k)
    if args.deep:
        # 下钻情景层：bigram 关键词匹配，不触发 touch
        results += store.deep_search(home, args.query, k=args.k)
    if not results:
        return EXIT_EMPTY
    for r in results:
        _emit(r, args.pretty)
    return EXIT_OK


def cmd_show(args) -> int:
    home = store.data_home()
    claim = store.replay(home).get(args.id)
    if claim is None:
        print(f"错误：找不到 claim {args.id}", file=sys.stderr)
        return EXIT_ERR
    _emit(_claim_out(claim), args.pretty)
    return EXIT_OK


def cmd_log(args) -> int:
    home = store.data_home()
    if args.text is not None:
        rec = store.log_write(home, args.text, topic=args.topic, entity=args.entity)
        _emit(rec, args.pretty)
        return EXIT_OK
    results = store.log_query(home, since=args.since, until=args.until,
                              topic=args.topic, entity=args.entity)
    if not results:
        return EXIT_EMPTY
    for r in results:
        _emit(r, args.pretty)
    return EXIT_OK


def cmd_profile(args) -> int:
    home = store.data_home()
    pack = (profile_mod.write_profile if args.write else profile_mod.build_profile)(
        home, budget=args.budget)
    if not pack["items"]:
        return EXIT_EMPTY
    if args.pretty:
        print(f"# profile  (tokens {pack['used_tokens']}/{pack['budget']})"
              + ("  → profile.md" if args.write else ""))
        for it in pack["items"]:
            print(f"- [{it.get('kind') or ''}|{it.get('entity') or ''}] "
                  f"({it.get('confidence')}, hits {it.get('hits', 0)}) {it['text']}")
    else:
        _emit(pack)
    return EXIT_OK


def cmd_query(args) -> int:
    home = store.data_home()
    claims = store.replay(home).values()
    results = []
    for c in claims:
        if args.status != "all" and c["status"] != args.status:
            continue
        if args.entity and c.get("entity") != args.entity:
            continue
        if args.kind and c.get("kind") != args.kind:
            continue
        if args.min_confidence is not None:
            rank = CONFIDENCE_RANK.get(c.get("confidence") or "", 0.0)
            if rank < args.min_confidence:
                continue
        results.append(_claim_out(c))
    if not results:
        return EXIT_EMPTY
    for r in results:
        _emit(r, args.pretty)
    return EXIT_OK


def cmd_context(args) -> int:
    home = store.data_home()
    pack = recall_mod.context_pack(home, args.task, budget=args.budget)
    if not pack["items"]:
        return EXIT_EMPTY
    if args.pretty:
        print(f"# context: {pack['task']}  (tokens {pack['used_tokens']}/{pack['budget']})")
        for it in pack["items"]:
            print(f"- {it['score']:.4f} [{it.get('kind') or ''}|{it.get('entity') or ''}] {it['text']}")
    else:
        _emit(pack)
    return EXIT_OK


def cmd_correct(args) -> int:
    home = store.data_home()
    new_claim = store.correct(home, args.id, args.text)
    if new_claim is None:
        print(f"错误：找不到活跃 claim {args.id}", file=sys.stderr)
        return EXIT_ERR
    embed.reindex(home)  # 文本变了，向量需重建（增量场景下全量重建足够快）
    _emit({"superseded": args.id, "new_id": new_claim["id"]}, args.pretty)
    return EXIT_OK


def cmd_forget(args) -> int:
    home = store.data_home()
    if not store.forget(home, args.id, hard=args.hard):
        print(f"错误：找不到活跃 claim {args.id}", file=sys.stderr)
        return EXIT_ERR
    _emit({"forgotten": args.id, "hard": args.hard}, args.pretty)
    return EXIT_OK


def cmd_restore(args) -> int:
    home = store.data_home()
    if not store.restore(home, args.id):
        print(f"错误：找不到可恢复的 claim {args.id}（不存在或已是活跃状态）", file=sys.stderr)
        return EXIT_ERR
    _emit({"restored": args.id}, args.pretty)
    return EXIT_OK


def cmd_status(args) -> int:
    home = store.data_home()
    all_claims = store.replay(home)
    n_active = sum(1 for c in all_claims.values() if c["status"] == "active")
    p = pressure(n_active)
    cp = store.claims_path(home)
    try:
        idx_ids, idx_vecs = embed.load_index(home) or ([], None)
        index = {"exists": idx_vecs is not None, "model": embed.MODEL_NAME, "count": len(idx_ids)}
    except embed.ModelMismatchError as e:
        index = {"exists": True, "error": str(e)}
    out = {
        "home": str(home),
        "active_claims": n_active,
        "total_claims": len(all_claims),
        "pressure": round(p, 4),
        "warm_limit": WARM_LIMIT,
        "index": index,
        "claims_file_bytes": cp.stat().st_size if cp.exists() else 0,
        "profile": _profile_info(home),
    }
    if p >= PRESSURE_WARN:
        out["warning"] = f"pressure {p:.2f} ≥ {PRESSURE_WARN}"
    _emit(out, args.pretty)
    return EXIT_OK


def cmd_reindex(args) -> int:
    n = embed.reindex(store.data_home())
    _emit({"reindexed": n, "model": embed.MODEL_NAME}, args.pretty)
    return EXIT_OK


def cmd_sleep(args) -> int:
    home = store.data_home()
    report = sleep_mod.run(home, dry_run=args.dry_run)
    clusters = report["merge_suggestions"]
    candidates = report["forget_candidates"]
    merged = report["near_duplicates"]
    if args.pretty:
        print(f"{'预览' if args.dry_run else '已执行'}："
              f"遗忘 {report.get('applied', {}).get('forgotten', len(candidates))} 条，"
              f"合并 {report.get('applied', {}).get('merged', len(merged))} 对")
        print(f"可合并簇 {len(clusters)} 个（建议，不自动执行）：")
        for cl in clusters:
            print(f"- {cl['entity']}：{cl['count']} 条")
            for c in cl["claims"]:
                print(f"    {c['id']}  {c['text'][:60]}")
        print(f"遗忘候选 {len(candidates)} 条（Ebbinghaus retention < 阈值）：")
        for cd in candidates:
            print(f"- {cd['id']}  retention={cd['retention']}  "
                  f"闲置{cd['days_idle']}天  {cd['text'][:50]}")
        print(f"近重复对 {len(merged)} 个：")
        for m in merged:
            print(f"- {m['merged']} → 并入 {m['kept']}  (sim {m['score']})")
        print(f"冲突带 {len(report['review_pairs'])} 对（0.75≤sim<0.95，需人工/agent 判断）：")
        for rp in report["review_pairs"]:
            print(f"- sim {rp['score']}  {rp['a']['id']} ⇄ {rp['b']['id']}")
            print(f"    A: {rp['a']['text'][:60]}")
            print(f"    B: {rp['b']['text'][:60]}")
        eps = report.get("episodic_candidates", [])
        print(f"情景提升候选 {len(eps)} 条（近 2 天 episodic × 语义库最近邻）：")
        for ec in eps:
            bm = ec["best_match"]
            match = f"≈ {bm['id']} (sim {bm['score']}) {bm['text'][:40]}" if bm else "（无近邻）"
            print(f"- {ec['ep']['id']}  {ec['ep']['text'][:50]}\n    {match}")
        if report.get("report_file"):
            print(f"报告已落盘：{report['report_file']}")
    else:
        _emit(report)
    return EXIT_OK if (clusters or candidates or merged or report["review_pairs"]
                       or report.get("episodic_candidates")) else EXIT_EMPTY


# ---------- argparse 骨架 ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memo", description="Agent 记忆系统 CLI")
    p.add_argument("--pretty", action="store_true", help="人类可读输出（默认 JSONL）")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("remember", help="写入一条记忆")
    sp.add_argument("text")
    sp.add_argument("--key", help="幂等键：同 key 旧值自动被 supersede")
    sp.add_argument("--kind")
    sp.add_argument("--entity")
    sp.add_argument("--source")
    sp.add_argument("--confidence", default="low", choices=list(CONFIDENCE_RANK))
    sp.set_defaults(func=cmd_remember)

    sp = sub.add_parser("recall", help="语义召回")
    sp.add_argument("query")
    sp.add_argument("-k", type=int, default=5)
    sp.add_argument("--deep", action="store_true", help="下钻 episodic 情景层（bigram 关键词匹配）")
    sp.set_defaults(func=cmd_recall)

    sp = sub.add_parser("log", help="写/查情景日志（有 TEXT=写入，无 TEXT=查询）")
    sp.add_argument("text", nargs="?", default=None)
    sp.add_argument("--topic")
    sp.add_argument("--entity")
    sp.add_argument("--since", help="查询起点 YYYY-MM-DD")
    sp.add_argument("--until", help="查询终点 YYYY-MM-DD")
    sp.set_defaults(func=cmd_log)

    sp = sub.add_parser("profile", help="生成 hot 层画像")
    sp.add_argument("--budget", type=int, default=HOT_TOKEN_BUDGET)
    sp.add_argument("--write", action="store_true", help="写入 ~/.memo/profile.md")
    sp.set_defaults(func=cmd_profile)

    sp = sub.add_parser("query", aliases=["list"], help="结构化字段过滤（不带参数 = 列出全部活跃记忆）")
    sp.add_argument("--entity")
    sp.add_argument("--kind")
    sp.add_argument("--status", default="active",
                    choices=["active", "superseded", "forgotten", "all"])
    sp.add_argument("--min-confidence", type=float, default=None)
    sp.set_defaults(func=cmd_query)

    sp = sub.add_parser("show", help="按 id 查看一条 claim（含非活跃状态）")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("context", help="为任务打包记忆画像")
    sp.add_argument("task")
    sp.add_argument("--budget", type=int, default=CONTEXT_DEFAULT_BUDGET)
    sp.set_defaults(func=cmd_context)

    sp = sub.add_parser("correct", help="修正一条记忆（supersede）")
    sp.add_argument("id")
    sp.add_argument("text")
    sp.set_defaults(func=cmd_correct)

    sp = sub.add_parser("forget", help="遗忘一条记忆")
    sp.add_argument("id")
    sp.add_argument("--hard", action="store_true", help="物理重写文件移除")
    sp.set_defaults(func=cmd_forget)

    sp = sub.add_parser("restore", help="回滚：恢复被软删/取代的记忆")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("status", help="容量与索引状态")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("reindex", help="全量重建向量索引")
    sp.set_defaults(func=cmd_reindex)

    sp = sub.add_parser("sleep", help="巩固：机械遗忘 + 近重复合并（--dry-run 只预览）")
    sp.add_argument("--dry-run", action="store_true", help="只输出预览，不做修改")
    sp.set_defaults(func=cmd_sleep)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except embed.ModelMismatchError as e:
        print(f"错误：{e}", file=sys.stderr)
        return EXIT_ERR
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
