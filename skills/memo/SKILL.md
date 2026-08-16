---
name: memo
description: Agent 的长期记忆系统。当对话涉及用户身份/项目/偏好的稳定事实、用户纠正 agent 的事实错误、用户说"记住/忘掉"、或回复前需要用户上下文时使用。底层是 memo CLI（JSONL + 向量召回），本 skill 规定调用纪律。
---

# memo — 长期记忆纪律

memo 是你的长期记忆：JSONL 存储 + bge 向量召回，全局命令直接调用：

```bash
memo <子命令> ...
```

（全局入口推荐做成 `~/.local/bin/memo` → `uv run --project <memo 仓库路径>` 的 shim；数据统一落在 `~/.memo/`，与 cwd 无关。）

退出码语义：**0 = 有结果，1 = 错误，2 = 无结果**。可用 `memo recall ... || memo remember ...` 形成"查不到就记"的惯用法。默认输出 JSONL（一行一条），`--pretty` 是全局参数，必须放在子命令**之前**（`memo --pretty recall ...`）。

## 记忆纪律（核心）

1. **回复前先取上下文**：当前话题涉及用户身份、项目或偏好时，先 `memo context "当前任务的一句话描述"` 一次，用返回的 claims 校准回复。
2. **自动记稳定事实**：对话中出现以下四类 → `memo remember`（confidence 默认 low，不用管）：
   - 身份变动（学校、职位、住址）→ 必须带 `--key`（如 `--key user.phd.institution`），key 化的记忆可寻址、自动覆盖旧值
   - 项目状态（开始、完成、转向）→ `--kind project --entity <项目名>`
   - 明确偏好（"我喜欢/讨厌/以后都……"）→ `--kind preference`
   - 用户纠正你的事实错误 → `memo correct <id> "正确表述"`，不要只在对话里道歉
   判据一句话：**三个月后这条信息还有用吗？** 有用就记；拿不准也记——重复和垃圾由 `memo sleep` 清理。近义重复会自动合并强化（返回 `merged_into`），不必担心啰嗦。
3. **显式记**：用户说"记住/别忘了"→ `memo remember ... --confidence high`。
4. **忘掉**：用户说"忘掉这个"→ `memo forget <id>` 并口头确认。id 从 recall/query/context 的返回里取。
5. **沉默记**：remember 成功不打断对话流，不播报"已记住"。用户可用 `memo query` 事后审计。
6. **冲突先问**：新事实与已有 high confidence 的 claim 直接矛盾时，先问用户再写——这种确认本身往往产生高价值记忆。
7. **不在会话中主动 `memo sleep`**：巩固由用户或定时任务触发。想预览就跑 `memo sleep --dry-run`。

## 命令速查

| 命令 | 用途 |
|---|---|
| `memo context "任务描述" [--budget 2000]` | 最常用：在 token 预算内打包与任务相关的记忆画像 |
| `memo recall "查询" [-k 5] [--deep]` | 语义召回；`--deep` 下钻情景日志层（默认只查主库） |
| `memo remember "事实" [--key K] [--kind K] [--entity E]` | 写入；有 key = 幂等更新，无 key = 追加（近义自动去重） |
| `memo query [--entity E --kind K] [--min-confidence 0.6]`（别名 `list`） | 结构化字段精确过滤；不带参数 = 列出全部活跃记忆 |
| `memo correct <id> "新表述"` | 修正：事实变了（旧 claim 保留谱系，强度归零） |
| `memo restate <id> "新表述"` | 再巩固：事实没变只刷新措辞（hits/confidence 继承，不清零） |
| `memo show <id>` | 按 id 查看单条 claim（含非活跃状态） |
| `memo forget <id> [--hard]` | 软删除；`--hard` 物理移除（审计仍留痕） |
| `memo restore <id>` | 回滚：恢复被软删/取代的记忆 |
| `memo log "事情" --topic T` | 写情景日志；`memo log --since ... --until ...` 查询 |
| `memo profile [--write]` | 生成 hot 层画像；`--write` 落盘 ~/.memo/profile.md |
| `memo status` | 库容量、pressure、索引与画像状态 |
| `memo sleep [--dry-run]` | 巩固：遗忘超期低置信 claim、合并近重复；dry-run 只预览 |
| `memo reindex` | 全量重建向量索引（索引只是缓存，坏了随时重建） |

## 注意

- pressure ≥ 0.8 时 remember 会返回警告，此时新写入要更挑剔，并提示用户跑 `memo sleep`。
- 情景日志层（`memo log`）记的是"发生了什么"，主库记的是"什么是真的"。事件流水进 log，稳定事实进 remember。
- 再巩固：recall/context 命中后发现某条措辞过时或含糊 → `memo restate <id> "更准确的表述"`。这继承 hits 与 confidence，是"刷新"；事实本身变了才用 `correct`（强度归零重新积累）。
- 文本中避免反引号：经 shell 调用时会被命令替换吞掉；长文本用单引号包裹。
