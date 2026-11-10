"""PII 脱敏：送 LLM 前与落库前两个卡点（P4-SEC-001）。

**双点脱敏的理由**：``clean_text`` 是要落库的字段，也是要拼进 ``user_prompt``
的字段。如果只在清洗阶段脱敏一次，后续任何绕过流水线的调用（例如评测脚本
直接拼 prompt）都可能把原文带出去。因此本模块提供两个**显式入口**：

- :func:`mask_for_storage`：落库前，产出 ``clean_text``。
- :func:`mask_for_llm` / :func:`assert_no_raw_pii`：送 LLM 前，二次校验 +
  兜底脱敏。

复用 :mod:`tagmind.core.security` 的 ``mask_pii()`` 与 ``PII_PATTERNS``，
不另起一套正则。``pii_fields`` **只记类型**（如 ``phone`` / ``email``），
绝不记原值；脱敏映射表记录的是 ``sha256(原值)``，无法反推。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from tagmind.core.errors import ValidationError
from tagmind.core.logging import get_logger
from tagmind.core.security import PII_PATTERNS, PII_TYPES, mask_pii

__all__ = [
    "PIIMaskRecord",
    "PIIMaskResult",
    "assert_no_raw_pii",
    "build_mask_map",
    "has_raw_pii",
    "mask_for_llm",
    "mask_for_storage",
]

logger = get_logger("tagmind.ingestion.pii")

#: 占位符形态：``[PHONE_1]`` / ``[ID_CARD_2]`` …
_PLACEHOLDER_RE = re.compile(r"\[([A-Z_]+)_(\d+)\]")


@dataclass(frozen=True, slots=True)
class PIIMaskResult:
    """一次脱敏的结果。

    Attributes:
        text: 脱敏后的文本。
        fields: 命中的 **PII 类型**（去重、排序），不含任何原值。
        placeholders: 生成的占位符列表，按出现顺序。
        count: 被替换的片段总数。
    """

    text: str
    fields: tuple[str, ...] = ()
    placeholders: tuple[str, ...] = ()
    count: int = 0

    @property
    def masked(self) -> bool:
        """是否命中过 PII。"""
        return self.count > 0

    @property
    def pii_fields(self) -> tuple[str, ...]:
        """落到 ``content.pii_fields`` 的值（只记类型）。"""
        return self.fields


@dataclass(frozen=True, slots=True)
class PIIMaskRecord:
    """脱敏映射表的一行（对应 ``pii_mask_map``）。

    ``original_hash`` 是 ``sha256(原值)``，**不存原值**（架构 §5.7）。
    """

    placeholder: str
    pii_type: str
    original_hash: str

    def to_dict(self) -> dict[str, str]:
        """转成字典（供仓储层写库）。"""
        return {
            "placeholder": self.placeholder,
            "pii_type": self.pii_type,
            "original_hash": self.original_hash,
        }


def _collect_originals(text: str) -> list[tuple[str, int, str]]:
    """按 ``mask_pii`` 的同一顺序收集 ``(类型, 序号, 原值)``。

    必须与 :func:`tagmind.core.security.mask_pii` 的类型遍历顺序、计数器
    规则完全一致，否则 ``build_mask_map`` 产出的占位符会和 ``mask_for_storage``
    产出的文本对不上。
    """
    result: list[tuple[str, int, str]] = []
    counters: dict[str, int] = {}
    working = text
    for pii_type in tuple(PII_PATTERNS):
        pattern = PII_PATTERNS[pii_type]

        def _walk(match: re.Match[str], kind: str = pii_type) -> str:
            counters[kind] = counters.get(kind, 0) + 1
            result.append((kind, counters[kind], match.group(0)))
            return f"[{kind.upper()}_{counters[kind]}]"

        working = pattern.sub(_walk, working)
    return result


def _result_from(text: str, masked: str, fields: list[str]) -> PIIMaskResult:
    """组装 :class:`PIIMaskResult`，从占位符反推命中数量与顺序。"""
    del text  # 原值不进入返回值，显式丢弃以满足 P4-SEC-001
    placeholders = tuple(match.group(0) for match in _PLACEHOLDER_RE.finditer(masked))
    return PIIMaskResult(
        text=masked,
        fields=tuple(sorted(set(fields))),
        placeholders=placeholders,
        count=len(placeholders),
    )


def mask_for_storage(text: str) -> PIIMaskResult:
    """落库前脱敏（卡点 1）。

    Args:
        text: 清洗后、尚未脱敏的正文。

    Returns:
        脱敏结果；``pii_fields`` 只含类型。
    """
    masked, fields = mask_pii(text)
    result = _result_from(text, masked, fields)
    if result.masked:
        logger.info(
            "pipeline.content.pii_masked",
            stage="ingest",
            checkpoint="storage",
            pii_field_count=len(result.fields),
            masked_count=result.count,
        )
    return result


def mask_for_llm(text: str) -> PIIMaskResult:
    """送 LLM 前脱敏（卡点 2）。

    对已经脱敏过的内容再跑一遍是安全的：占位符不含手机号/邮箱等模式，
    ``mask_pii`` 不会产生二次替换。这样即使调用方绕过流水线直接拼 prompt，
    也不会把原文带出去。

    Args:
        text: 即将进入 ``user_prompt`` 的文本。

    Returns:
        脱敏结果。
    """
    masked, fields = mask_pii(text)
    result = _result_from(text, masked, fields)
    if result.masked:
        logger.warning(
            "pipeline.content.pii_masked",
            stage="tag",
            checkpoint="llm",
            masked_count=result.count,
        )
    return result


def has_raw_pii(text: str) -> bool:
    """判断文本中是否仍含原始 PII（占位符不算）。"""
    return any(pattern.search(text) for pattern in PII_PATTERNS.values())


def assert_no_raw_pii(text: str, *, context: str = "llm_request") -> None:
    """送 LLM 前的硬性闸门：发现原始 PII 直接报错。

    Args:
        text: 待检查文本。
        context: 上下文标识，写进错误详情便于定位。

    Raises:
        ValidationError: 仍含原始 PII——**绝不**静默外发。
    """
    if has_raw_pii(text):
        raise ValidationError(
            "送 LLM 的内容仍含未脱敏 PII，已阻断外发",
            details={"context": context, "checkpoint": "llm", "pii_types": list(PII_TYPES)},
        )


def build_mask_map(original: str) -> tuple[PIIMaskRecord, ...]:
    """构造脱敏映射记录（只存哈希）。

    Args:
        original: **原始**文本（含 PII）。

    Returns:
        占位符 → 类型 → ``sha256(原值)`` 的记录列表，顺序稳定。
    """
    records: list[PIIMaskRecord] = []
    for pii_type, index, value in _collect_originals(original):
        records.append(
            PIIMaskRecord(
                placeholder=f"[{pii_type.upper()}_{index}]",
                pii_type=pii_type,
                original_hash=hashlib.sha256(value.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(records)
