# memo

Agent 记忆系统 CLI：JSONL append-only 为唯一 source of truth，fastembed（bge-small-zh-v1.5）做本地语义召回，向量索引是可重建的派生缓存。

## 安装与运行

```bash
uv sync
uv run memo <command>
```

全局入口（推荐）：建一个 shim，任意目录直接 `memo <command>`：

```bash
cat > ~/.local/bin/memo <<'EOF'
#!/bin/sh
exec uv run --project /path/to/memo memo "$@"
EOF
chmod +x ~/.local/bin/memo
```

数据目录默认 `~/.memo/`（全局唯一，与 cwd 无关），可用环境变量 `MEMO_HOME` 覆盖。

## Agent skill

`skills/memo/SKILL.md` 是给 agent 的调用纪律（何时取上下文、何时记/改/忘），复制到 agent 的 skill 目录即可（如 kimi-code 的 `~/.kimi-code/skills/memo/`）。CLI 只是工具，纪律才是核心。

## 命令

| 命令 | 说明 |
|---|---|
| `memo remember TEXT [--key K] [--kind K] [--entity E] [--source S] [--confidence L]` | 写入记忆；同 key 旧值自动 supersede；无 key 时近义（sim≥0.9）自动合并强化旧 claim |
| `memo recall QUERY [-k 5] [--deep]` | 语义召回 top-k；`--deep` 下钻 episodic 情景层（bigram 匹配） |
| `memo log [TEXT] [--topic T] [--entity E] [--since D] [--until D]` | 有 TEXT=写情景日志（按月分片），无 TEXT=查询 |
| `memo query [--entity E] [--kind K] [--status S] [--min-confidence F]`（别名 `list`） | 结构化过滤；不带参数 = 列出全部活跃记忆 |
| `memo context TASK [--budget 2000]` | recall top-20 按 token 预算打包 |
| `memo profile [--budget 500] [--write]` | 生成 hot 层画像；`--write` 落盘 `~/.memo/profile.md` |
| `memo correct ID TEXT` | 修正（supersede，保留谱系，强度归零） |
| `memo restate ID TEXT` | 再巩固：重述表述但继承 hits/confidence/年龄（召回后顺手刷新措辞用） |
| `memo show ID` | 按 id 查看单条 claim（含已被取代/遗忘的） |
| `memo forget ID [--hard]` | 软删；--hard 物理移除 |
| `memo restore ID` | 回滚：恢复被软删/取代的记忆 |
| `memo status` | 容量 pressure、索引与画像状态 |
| `memo reindex` | 全量重建向量索引 |
| `memo sleep [--dry-run]` | 巩固：Ebbinghaus 衰减遗忘（retention<0.2 且 low）、近重复合并（sim≥0.95）；冲突带 [0.75,0.95) 与同 entity 簇只报告（自带文本） |

退出码：0 有结果 / 1 错误 / 2 无结果。默认输出 JSONL；`--pretty` 是全局参数，放在子命令之前（`memo --pretty status`）。

confidence 自动升级：claim 的 hits 累计 ≥3 升 mid、≥8 升 high（recall 命中与去重合并都会累加 hits）。

## 测试

```bash
uv run pytest
```
