"""可调阈值集中定义。

**为什么单独一个模块**：层权、IDF 开关、质量阈值这些数字会同时影响
「标签向量」「质量闸门」「冲突检测」「画像衰减」四条链路。把它们散落在各个
模块里，改一个数字就要在四个文件里找——而且改了之后 ``tag_space_version``
不会变，导致新旧分数被混算（架构 §11.2 的警告）。

因此：凡影响召回分数的阈值，都必须在此处定义，并计入对应的版本串。
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "ARBITRATION_MIN_CONFIDENCE",
    "CONFLICT_SIM_THRESHOLD",
    "CONFLICT_TOP_K",
    "CONTENT_DRIFT_TV_THRESHOLD",
    "DRIFT_TV_THRESHOLD",
    "IDF_ENABLED",
    "IDF_SMOOTHING",
    "LAYER_WEIGHTS",
    "MAX_CONTENT_CHARS",
    "MIN_CONFIDENCE",
    "PROFILE_COLD_START_THRESHOLD",
    "PROFILE_DECAY_HALFLIFE_DAYS",
    "PROMPT_TOKEN_BUDGET_MAX",
    "PROMPT_TOKEN_BUDGET_MIN",
    "QUALITY_MIN_SCORE",
    "QUALITY_REJECT_SCORE",
    "QUALITY_REVIEW_SCORE",
    "REC_EXPLORE_RATIO",
    "REC_MIN_INTERACTIONS",
    "TAG_VECTOR_NORM",
    "WEIGHT_SUM_TOLERANCE",
]

#: C7 置信度门槛：低于此值的标签不进 ``labels``，但保留在 ``label_raw``。
MIN_CONFIDENCE: Final[float] = 0.7

#: C8 仲裁置信度门槛：低于此值转人工复核。
ARBITRATION_MIN_CONFIDENCE: Final[float] = 0.75

#: 质量分三档阈值（PRD P4-QA-003：阈值 4.0 / 6.0 时，
#: 2.0 → rejected、5.0 → needs_review、8.0 → approved）。
QUALITY_REJECT_SCORE: Final[float] = 4.0
QUALITY_REVIEW_SCORE: Final[float] = 6.0
QUALITY_MIN_SCORE: Final[float] = 8.0

#: 冲突检索 top-k。
CONFLICT_TOP_K: Final[int] = 5

#: 语义冲突的相似度阈值。
CONFLICT_SIM_THRESHOLD: Final[float] = 0.82

#: 单条内容送入 LLM 的最大字符数。
MAX_CONTENT_CHARS: Final[int] = 6000

#: 提示词 token 预算区间（ADR-0002：1.5–2 万 token）。
PROMPT_TOKEN_BUDGET_MIN: Final[int] = 15000
PROMPT_TOKEN_BUDGET_MAX: Final[int] = 20000

#: 层权表（架构 §11.2）。层越浅语义越强，权重越高。
#:
#: 变更本表会改变 ``tag_space_version`` → 触发全量 ``tag_vector`` 重建
#: → 必须走灰度，否则线上召回分数漂移不可解释。
LAYER_WEIGHTS: Final[dict[int, float]] = {
    1: 1.00,
    2: 1.00,
    3: 0.80,
    4: 0.80,
    5: 0.55,
    6: 0.55,
    7: 0.55,
    8: 0.55,
    9: 0.55,
    10: 0.30,
    11: 0.75,
    12: 0.75,
}

#: 是否启用 IDF 归一化。关闭时退化为纯层权加权。
IDF_ENABLED: Final[bool] = True

#: IDF 平滑项（架构 §11.3）：``idf = ln((N+1)/(df+1)) + 1``。
IDF_SMOOTHING: Final[int] = 1

#: 标签向量的归一化方式：``l2`` 或 ``none``。
TAG_VECTOR_NORM: Final[str] = "l2"

#: 归一化权重之和的允许误差（架构 §13.2 的 V6）。
WEIGHT_SUM_TOLERANCE: Final[float] = 1e-6

#: 画像冷启动阈值：累计有效行为数达到该值即脱离冷启动。
PROFILE_COLD_START_THRESHOLD: Final[int] = 10

#: 画像半衰期（天）。``λ = ln2 / (半衰期 × 86400)``。
PROFILE_DECAY_HALFLIFE_DAYS: Final[int] = 14

#: 用户兴趣漂移阈值（TV 距离，架构 §6.4）。
DRIFT_TV_THRESHOLD: Final[float] = 0.20

#: 内容层漂移阈值（TV 距离，架构 §6.4）。
CONTENT_DRIFT_TV_THRESHOLD: Final[float] = 0.15

#: 协同过滤的最小有效交互数（歧义 A-09 裁决）。
REC_MIN_INTERACTIONS: Final[int] = 20

#: 探索位占比。
REC_EXPLORE_RATIO: Final[float] = 0.1
