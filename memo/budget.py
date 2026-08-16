"""容量常量与 pressure 计算。"""

WARM_LIMIT = 3000  # claims 硬上限
PRESSURE_WARN = 0.8  # 超过则在 remember 输出里带警告
CONTEXT_DEFAULT_BUDGET = 2000  # context 命令的默认 token 预算
HOT_TOKEN_BUDGET = 500  # hot 层画像的 token 预算

DEDUP_THRESHOLD = 0.9  # remember 时相似度 ≥ 此值则强化旧 claim 而非新增
MERGE_THRESHOLD = 0.95  # sleep 时相似度 ≥ 此值的对自动合并
REVIEW_SIM_LOW = 0.75  # sleep 冲突带下界：[此值, MERGE_THRESHOLD) 的对报给 LLM 审
CONFIDENCE_MID_HITS = 3  # hits 累计到此值 low → mid
CONFIDENCE_HIGH_HITS = 8  # hits 累计到此值 mid → high

# Ebbinghaus 衰减：retention = exp(-未激活天数 / (DECAY_TAU × (1+hits)))
# retention < RETENTION_FLOOR 且 low confidence → sleep 遗忘候选
DECAY_TAU = 30.0
RETENTION_FLOOR = 0.2


def pressure(active_count: int) -> float:
    """当前活跃 claims 占 warm 层容量的比例。"""
    return active_count / WARM_LIMIT
