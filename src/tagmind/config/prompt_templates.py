"""提示词模板与版本号。

四套模板各自独立版本化（架构 §14.7 / PRD §4.6）：

- ``SYSTEM``：角色定义 + 标签体系 + 规则约束 + 输出 Schema。
- ``USER``：待标内容 + 辅助信息 + few-shot 占位。
- ``ARBITER``：冲突仲裁的 CoT 模板。
- ``SCORING``：质量评分的理由锚定模板。

**版本号变更规则**：文本一字不改 → 版本号不变；文本有任何改动 → 版本号必须
递增。否则 ``llm_call_audit.prompt_hash`` 与版本号会脱节，审计还原失效
（P4-LLM-AUDIT-001）。

标签体系（``label_def.version``）与提示词（``prompt_version``）是**两条独立的
版本线**：运营改标签不应迫使算法改提示词，反之亦然。这是灰度双版本能成立的
前提。
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "ARBITER_PROMPT_VERSION",
    "ARBITER_TEMPLATE",
    "OUTPUT_SCHEMA",
    "SCORING_PROMPT_VERSION",
    "SCORING_TEMPLATE",
    "SYSTEM_PROMPT_VERSION",
    "SYSTEM_TEMPLATE",
    "USER_PROMPT_VERSION",
    "USER_TEMPLATE",
    "all_prompt_versions",
]

#: system 提示词版本号。
SYSTEM_PROMPT_VERSION: Final[str] = "pt_system_v1"

#: user 提示词版本号。
USER_PROMPT_VERSION: Final[str] = "pt_user_v1"

#: 仲裁提示词版本号。
ARBITER_PROMPT_VERSION: Final[str] = "pt_arbiter_v1"

#: 评分提示词版本号。
SCORING_PROMPT_VERSION: Final[str] = "pt_scoring_v1"

#: 打标 system 模板。占位符由 :mod:`tagmind.labeling.prompt_builder` 填充。
SYSTEM_TEMPLATE: Final[str] = """你是内容标注引擎。你只输出 JSON，不输出任何解释性文字。

## 标签体系

{taxonomy_block}

## 硬约束

以下约束必须全部满足，否则你的输出会被后处理层修复或拒绝：

1. C1 树层唯一性：L1 与 L2 各恰好命中 1 条；L3、L4 同父节点下最多命中 1 条。
2. C2 同父互斥：同一父节点下的兄弟标签不得同时命中。
3. C3 祖先闭包：命中第 k 层标签时，L1 到 L(k-1) 层的祖先标签也要一并给出。
4. C5 每层配额：每层命中数不得超过该层配额。
5. C6 显式互斥组：同一互斥组内最多命中 1 条。
6. C7 置信度门槛：confidence 必须落在 0 到 1 之间；低于 {min_confidence} 的标签不要输出。
7. C10 版本一致性：所有标签都来自同一套标签体系版本。

## 输出格式

严格输出如下 JSON，不要包裹 markdown 代码块：

{schema_block}

## 冲突示例

违反 C2 的输出：同时给出「存储芯片」与「先进封装」（二者同属「半导体与芯片」）。
违反 C6 的输出：同时给出「正面情绪」与「负面情绪」（二者同属情感互斥组）。
"""

#: 打标 user 模板。
USER_TEMPLATE: Final[str] = """## 待标内容

以下内容已完成清洗与脱敏，占位符 `[PHONE_1]` 之类代表已被抹除的敏感信息。

{content_block}

## 辅助信息

- 来源：{source}
- 时间：{published_at}
- 语言：{language}
- 内容类型：{content_type}

## 任务

为上述内容选择标签，输出 JSON。
"""

#: 仲裁模板（CoT：先分析再结论）。
ARBITER_TEMPLATE: Final[str] = """你是标签冲突仲裁器。下面两个候选标签互斥，你需要选出更贴切的一个。

## 候选标签

{candidate_block}

## 内容片段

{content_block}

## 参考证据

{evidence_block}

## 思考步骤

1. 先用一句话概括内容的主���。
2. 对比两个候选标签各自的核心语义与主���的匹配度。
3. 指出主���更贴切以及理由。

## 输出

严格输出如下 JSON：

{schema_block}
"""

#: 质量评分模板（理由锚定：必须引用具体依据）。
SCORING_TEMPLATE: Final[str] = """你是内容质量评分器。请为下面这段内容打 0 到 10 分。

## 内容

{content_block}

## 评分维度

- 信息量：是否提供了实质信息而非空洞套话。
- 完整度：论述是否闭环，有无明显残缺。
- 可读性：结构是否清晰，有无严重错乱。
- 原创性：是否只是复制粘贴的通稿。

## 要求

分数必须在 0 到 10 之间（可带一位小数）。
理由必须引用内容中的具体依据（引用片段或指明缺失的部分），不得只写「质量良好」。

{schema_block}
"""

#: 打标输出的 JSON Schema。``temperature=0`` 下模型仍可能跑偏，
#: 因此解析器必须做容错修复（阶段 1 的 :mod:`tagmind.labeling.parser`）。
OUTPUT_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "required": ["labels"],
    "additionalProperties": False,
    "properties": {
        "labels": {
            "type": "array",
            "description": "命中的标签列表",
            "items": {
                "type": "object",
                "required": ["code", "layer", "confidence"],
                "additionalProperties": False,
                "properties": {
                    "code": {"type": "string", "description": "标签稳定编码"},
                    "layer": {"type": "integer", "minimum": 1, "maximum": 12},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
        "summary": {"type": "string", "description": "一句话内容摘要"},
    },
}

#: 仲裁输出的 JSON Schema。
ARBITRATION_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "required": ["winner", "reason", "confidence"],
    "additionalProperties": False,
    "properties": {
        "winner": {"type": "string", "description": "胜出的标签 code"},
        "reason": {"type": "string", "minLength": 20, "description": "仲裁理由，至少 20 字符"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

#: 质量评分的 JSON Schema。
SCORING_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "required": ["score", "reason"],
    "additionalProperties": False,
    "properties": {
        "score": {"type": "number", "minimum": 0, "maximum": 10},
        "reason": {"type": "string", "minLength": 10, "description": "评分理由"},
    },
}


def all_prompt_versions() -> dict[str, str]:
    """返回四套模板的版本号。

    Returns:
        模板名 → 版本号。
    """
    return {
        "system": SYSTEM_PROMPT_VERSION,
        "user": USER_PROMPT_VERSION,
        "arbiter": ARBITER_PROMPT_VERSION,
        "scoring": SCORING_PROMPT_VERSION,
    }
