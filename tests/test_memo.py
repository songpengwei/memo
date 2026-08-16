"""端到端测试：remember → recall → query → correct → forget → status / sleep。

全程用 MEMO_HOME 指向 tmp_path 隔离，不碰真实 ~/.memo。
首次运行会下载 bge-small-zh-v1.5 模型（约 100MB）。
"""
import json

import pytest

from memo import cli, store


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    return tmp_path


def run(capsys, *argv):
    """执行 CLI 并解析 JSONL 输出，返回 (exit_code, [records])。"""
    code = cli.main(list(argv))
    out = capsys.readouterr().out
    records = [json.loads(line) for line in out.splitlines() if line.strip()]
    return code, records


def test_remember_and_pressure(home, capsys):
    code, recs = run(capsys, "remember", "用户开始研究解州关帝庙藻井测绘",
                     "--entity", "关帝庙", "--kind", "project")
    assert code == 0
    assert recs[0]["id"].startswith("mem_")
    assert recs[0]["pressure"] == round(1 / 3000, 4)  # 输出保留 4 位小数
    assert recs[0]["active_claims"] == 1
    # 目录结构就位
    assert (home / "claims.jsonl").exists()
    assert (home / "audit.jsonl").exists()
    assert (home / "episodic").is_dir()
    assert (home / "index" / "vecs.npy").exists()
    assert (home / "index" / "ids.json").exists()


def test_key_idempotent_update(home, capsys):
    run(capsys, "remember", "用户偏好简体中文回复", "--key", "lang")
    code, recs = run(capsys, "remember", "用户偏好简体中文，少用emoji", "--key", "lang")
    assert code == 0
    assert recs[0]["active_claims"] == 1  # 旧值被自动 supersede，活跃仍只有 1 条
    active = store.active_claims(home)
    assert len(active) == 1
    assert active[0]["text"] == "用户偏好简体中文，少用emoji"
    # 旧 claim 谱系保留
    all_claims = store.replay(home)
    assert any(c["status"] == "superseded" for c in all_claims.values())


def test_recall_semantic_hit(home, capsys):
    run(capsys, "remember", "用户开始研究解州关帝庙藻井测绘", "--entity", "关帝庙", "--kind", "project")
    run(capsys, "remember", "冰箱里牛奶快过期了", "--kind", "life")
    code, recs = run(capsys, "recall", "用户在研究什么古建筑", "-k", "2")
    assert code == 0
    assert recs[0]["entity"] == "关帝庙"
    assert recs[0]["score"] > recs[-1]["score"] or len(recs) == 1
    # 命中后 hits / last_accessed 已更新
    claim = store.replay(home)[recs[0]["id"]]
    assert claim["hits"] == 1
    assert claim["last_accessed"] is not None


def test_recall_empty(home, capsys):
    code, _ = run(capsys, "recall", "任意问题")
    assert code == 2


def test_query_filters(home, capsys):
    run(capsys, "remember", "用户开始研究解州关帝庙藻井测绘", "--entity", "关帝庙", "--kind", "project")
    run(capsys, "remember", "用户计划去奉国寺踩点", "--entity", "奉国寺", "--kind", "project")
    code, recs = run(capsys, "query", "--entity", "关帝庙")
    assert code == 0
    assert len(recs) == 1
    assert recs[0]["entity"] == "关帝庙"
    code, recs = run(capsys, "query", "--entity", "不存在")
    assert code == 2
    code, recs = run(capsys, "query", "--kind", "project")
    assert code == 0 and len(recs) == 2


def test_correct(home, capsys):
    _, recs = run(capsys, "remember", "用户研究应县木塔", "--entity", "木塔")
    old_id = recs[0]["id"]
    code, recs = run(capsys, "correct", old_id, "用户研究的是解州关帝庙而非应县木塔")
    assert code == 0
    assert recs[0]["superseded"] == old_id
    all_claims = store.replay(home)
    assert all_claims[old_id]["status"] == "superseded"  # 谱系不删行
    new_claim = all_claims[recs[0]["new_id"]]
    assert new_claim["status"] == "active"
    assert new_claim["entity"] == "木塔"  # 继任 claim 继承旧字段
    # correct 不存在的 id 报错
    code, _ = run(capsys, "correct", "mem_xxxx", "随便")
    assert code == 1


def test_forget_soft_and_hard(home, capsys):
    _, r1 = run(capsys, "remember", "临时的购物清单", "--kind", "life")
    _, r2 = run(capsys, "remember", "另一条临时记录", "--kind", "life")
    soft_id, hard_id = r1[0]["id"], r2[0]["id"]

    code, _ = run(capsys, "forget", soft_id)
    assert code == 0
    claims = store.replay(home)
    assert claims[soft_id]["status"] == "forgotten"
    # 软删后不再出现在默认 query
    code, recs = run(capsys, "query")
    assert all(r["id"] != soft_id for r in recs)
    # --status forgotten 能看到
    code, recs = run(capsys, "query", "--status", "forgotten")
    assert code == 0 and recs[0]["id"] == soft_id

    # --hard 物理移除：文件里不再出现该 id
    code, _ = run(capsys, "forget", hard_id, "--hard")
    assert code == 0
    raw = (home / "claims.jsonl").read_text(encoding="utf-8")
    assert hard_id not in raw
    assert hard_id not in store.replay(home)
    # 重复 forget 报错
    code, _ = run(capsys, "forget", hard_id)
    assert code == 1


def test_status(home, capsys):
    run(capsys, "remember", "用户开始研究解州关帝庙藻井测绘")
    code, recs = run(capsys, "status")
    assert code == 0
    s = recs[0]
    assert s["active_claims"] == 1
    assert s["pressure"] == round(1 / 3000, 4)
    assert s["warm_limit"] == 3000
    assert s["index"]["exists"] is True
    assert s["index"]["model"] == "BAAI/bge-small-zh-v1.5"
    assert s["claims_file_bytes"] > 0


def test_context_budget(home, capsys):
    for i in range(5):
        # --key 跳过 remember 去重，保证 5 条独立 claim
        run(capsys, "remember", f"关帝庙测绘笔记第{i}条，斗拱与藻井细节记录",
            "--entity", "关帝庙", "--key", f"note{i}")
    code, recs = run(capsys, "context", "帮用户策划关帝庙视频", "--budget", "100")
    assert code == 0
    pack = recs[0]
    assert pack["budget"] == 100
    assert pack["used_tokens"] <= 100 + 20  # 单条超额也至少保留一条
    assert 1 <= len(pack["items"]) <= 20
    # 分数降序
    scores = [it["score"] for it in pack["items"]]
    assert scores == sorted(scores, reverse=True)


def test_sleep_dry_run(home, capsys):
    for i in range(3):
        run(capsys, "remember", f"关帝庙相关资料第{i}条", "--entity", "关帝庙",
            "--key", f"res{i}")  # --key 跳过去重，保证 3 条独立 claim
    code, recs = run(capsys, "sleep", "--dry-run")
    assert code == 0
    out = recs[0]
    assert out["dry_run"] is True
    assert len(out["merge_suggestions"]) == 1
    assert out["merge_suggestions"][0]["entity"] == "关帝庙"
    assert out["merge_suggestions"][0]["count"] == 3
    # 新建的 claims 不应进入 90 天遗忘候选
    assert out["forget_candidates"] == []
    # dry-run 不产生任何修改
    assert len(store.active_claims(home)) == 3


def test_sleep_forget_candidates(home, capsys, monkeypatch):
    # 手工造一条 100 天前的 claim
    claim = store.remember(home, "很久以前的老记录")
    claim["created"] = "2026-04-01T00:00:00Z"
    line = json.dumps(claim, ensure_ascii=False)
    (home / "claims.jsonl").write_text(line + "\n", encoding="utf-8")
    code, recs = run(capsys, "sleep", "--dry-run")
    assert code == 0
    assert len(recs[0]["forget_candidates"]) == 1
    assert recs[0]["forget_candidates"][0]["id"] == claim["id"]


# ---------- v1.5：去重 / confidence 升级 / sleep 真执行 / episodic / profile ----------

def _make_old(home, text, days_ago=100, confidence="low"):
    """手工造一条 days_ago 天前的 claim（绕过 CLI 直接写文件）。"""
    from datetime import datetime, timedelta, timezone
    claim = store.remember(home, text, confidence=confidence)
    claim["created"] = (datetime.now(timezone.utc) - timedelta(days=days_ago)
                        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    (home / "claims.jsonl").write_text(
        json.dumps(claim, ensure_ascii=False) + "\n", encoding="utf-8")
    return claim


def test_remember_dedup(home, capsys):
    _, r1 = run(capsys, "remember", "用户喜欢中国古建筑", "--kind", "preference")
    code, r2 = run(capsys, "remember", "用户喜欢中国古建筑", "--kind", "preference")
    assert code == 0
    assert r2[0]["merged_into"] == r1[0]["id"]  # 近义 → 强化旧 claim
    assert len(store.active_claims(home)) == 1  # 库不膨胀
    assert store.replay(home)[r1[0]["id"]]["hits"] == 1


def test_confidence_auto_upgrade(home):
    claim = store.remember(home, "用户偏好简体中文回复")
    cid = claim["id"]
    assert store.replay(home)[cid]["confidence"] == "low"
    for _ in range(3):
        store.touch(home, cid)
    assert store.replay(home)[cid]["confidence"] == "mid"
    for _ in range(5):
        store.touch(home, cid)
    assert store.replay(home)[cid]["confidence"] == "high"
    # 谱系里有 confidence 操作行
    raw = (home / "claims.jsonl").read_text(encoding="utf-8")
    assert '"op": "confidence"' in raw


def test_sleep_real_forget(home, capsys):
    claim = _make_old(home, "一百天前且从未被想起的琐事")
    code, recs = run(capsys, "sleep")
    assert code == 0
    assert recs[0]["dry_run"] is False
    assert recs[0]["applied"]["forgotten"] == 1
    assert store.replay(home)[claim["id"]]["status"] == "forgotten"
    # high confidence 的老 claim 不被遗忘
    claim2 = _make_old(home, "一百天前的重要事实", confidence="high")
    run(capsys, "sleep")
    assert store.replay(home)[claim2["id"]]["status"] == "active"


def test_retention_ebbinghaus():
    """Ebbinghaus 曲线：闲置越久保持度越低，hits 越多衰减越慢。"""
    from datetime import datetime, timedelta, timezone
    from memo.sleep import retention
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    c = lambda created, hits, accessed=None: {
        "created": created, "hits": hits, "last_accessed": accessed}
    # 100 天零命中 → 低于遗忘线；5 天 → 远高于
    assert retention(c(old, 0), now) < 0.2
    assert retention(c(fresh, 0), now) > 0.8
    # 同样闲置 100 天，hits=9 的稳定度足以留在阈上
    assert retention(c(old, 9), now) > retention(c(old, 0), now)
    assert retention(c(old, 9), now) > 0.2
    # 最近被召回过的老记忆保持度回升（按 last_accessed 而非 created）
    assert retention(c(old, 0, accessed=fresh), now) > 0.8


def test_sleep_review_pairs_band(home, capsys, monkeypatch):
    """冲突带 [0.75, 0.95)：只报告不合并；≥0.95 才自动合并。"""
    from memo import sleep as sleep_mod
    _, r1 = run(capsys, "remember", "用户在澳门大学读博士", "--key", "k1")
    _, r2 = run(capsys, "remember", "用户计划去葡萄牙旅行", "--key", "k2")
    _, r3 = run(capsys, "remember", "用户在澳门大学读博士。", "--key", "k3")
    claims = {c["id"]: c for c in store.active_claims(home)}
    fake_pairs = [
        (claims[r1[0]["id"]], claims[r2[0]["id"]], 0.82),   # 冲突带 → 审阅
        (claims[r1[0]["id"]], claims[r3[0]["id"]], 0.97),   # 近重复 → 自动合并
    ]
    monkeypatch.setattr(sleep_mod, "_sim_pairs", lambda h, a: fake_pairs)
    code, recs = run(capsys, "sleep")
    assert code == 0
    out = recs[0]
    assert len(out["review_pairs"]) == 1
    assert out["review_pairs"][0]["score"] == 0.82
    assert out["review_pairs"][0]["a"]["text"]  # 带文本，审阅不用再 show
    assert len(out["near_duplicates"]) == 1
    # 冲突带的两条都还在；近重复的被并掉一条
    claims_after = store.replay(home)
    assert claims_after[r1[0]["id"]]["status"] == "active"
    assert claims_after[r2[0]["id"]]["status"] == "active"
    assert claims_after[r3[0]["id"]]["status"] == "superseded"


def test_restore_rollback(home, capsys):
    """软删/取代均可回滚：append-only 的逆操作。"""
    _, r = run(capsys, "remember", "一条会被误删的记忆", "--kind", "test")
    cid = r[0]["id"]
    run(capsys, "forget", cid)
    assert store.replay(home)[cid]["status"] == "forgotten"
    code, recs = run(capsys, "restore", cid)
    assert code == 0 and recs[0]["restored"] == cid
    assert store.replay(home)[cid]["status"] == "active"
    # 活跃状态的 restore 报错
    code, _ = run(capsys, "restore", cid)
    assert code == 1


def test_sleep_report_and_episodic(home, capsys):
    """真执行 sleep：落盘巩固报告 + 情景日志进入提升候选。"""
    run(capsys, "remember", "用户在研究解州关帝庙", "--entity", "关帝庙")
    run(capsys, "log", "今天查了关帝庙藻井的测绘资料", "--topic", "访古")
    code, recs = run(capsys, "sleep")
    assert code == 0
    out = recs[0]
    # 报告落盘（可审计的 diff）
    report_file = out["report_file"]
    saved = json.loads(open(report_file, encoding="utf-8").read())
    assert saved["applied"] == out["applied"]
    assert saved["ts"]
    # episodic 候选带语义库最近邻
    eps = out["episodic_candidates"]
    assert len(eps) == 1
    assert eps[0]["ep"]["topic"] == "访古"
    assert eps[0]["best_match"]["entity"] == "关帝庙"
    # dry-run 不落盘
    code, recs = run(capsys, "sleep", "--dry-run")
    assert "report_file" not in recs[0]


def test_sleep_near_dup_merge(home, capsys):
    # --key 跳过 remember 去重，人为制造两条近重复
    _, r1 = run(capsys, "remember", "用户在澳门大学读博士", "--key", "k1")
    _, r2 = run(capsys, "remember", "用户在澳门大学读博士。", "--key", "k2")
    code, recs = run(capsys, "sleep")
    assert code == 0
    dups = recs[0]["near_duplicates"]
    assert len(dups) == 1
    claims = store.replay(home)
    assert claims[dups[0]["merged"]]["status"] == "superseded"
    assert claims[dups[0]["kept"]]["status"] == "active"


def test_log_write_and_query(home, capsys):
    code, recs = run(capsys, "log", "在义县踩点奉国寺，拍了大雄殿七佛",
                     "--topic", "义县", "--entity", "奉国寺")
    assert code == 0
    assert recs[0]["id"].startswith("ep_")
    assert (home / "episodic").glob("*.jsonl")
    code, recs = run(capsys, "log", "--topic", "义县")
    assert code == 0 and len(recs) == 1
    assert recs[0]["entity"] == "奉国寺"
    code, _ = run(capsys, "log", "--since", "2999-01-01")
    assert code == 2


def test_recall_deep(home, capsys):
    run(capsys, "log", "上周在义县看了奉国寺大雄殿的辽代彩塑", "--topic", "义县")
    code, recs = run(capsys, "recall", "义县辽代彩塑", "--deep", "-k", "3")
    assert code == 0
    episodic = [r for r in recs if r.get("layer") == "episodic"]
    assert len(episodic) == 1
    assert "奉国寺" in episodic[0]["text"]


def test_profile(home, capsys):
    run(capsys, "remember", "用户在澳门大学读博士", "--key", "edu",
        "--confidence", "high")
    run(capsys, "remember", "用户开始研究关帝庙测绘", "--kind", "project")
    code, recs = run(capsys, "profile")
    assert code == 0
    items = recs[0]["items"]
    assert items[0]["confidence"] == "high"  # 高置信排前
    assert items[0]["text"] == "用户在澳门大学读博士"
    # --write 落盘 + status 可见
    code, _ = run(capsys, "profile", "--write")
    assert code == 0
    content = (home / "profile.md").read_text(encoding="utf-8")
    assert "澳门大学" in content
    code, recs = run(capsys, "status")
    assert recs[0]["profile"]["exists"] is True
    assert recs[0]["profile"]["count"] == 2
