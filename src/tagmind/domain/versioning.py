"""版本与口径领域模型：PipelineVersion / LabelDefVersion / PromptVersion。

**版本绑定是诚实口径的代码化**（架构 §6.3 / ADR-0010）：
任何指标都必须绑定产生它的 ``label_def.version`` / ``model_version`` /
``prompt_version`` / ``pipeline_version``，否则跨版本比较无从解释。

``MetricValue`` 与 ``EvidenceType`` 同样定义在本模块——它们是「绑定版本的值对象」，
与 :class:`PipelineVersion` 同属一个语义单元。ADR-0010 里把它们写在
``domain/metrics.py``，但架构 §4.1 的可施工清单中该文件不存在（阶段 0 的
``domain/`` 只有 10 个模块），因此归并到本模块，避免出现清单外的文件。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from tagmind.domain.base import DomainModel, JsonObject

__all__ = [
    "EvidenceType",
    "LabelDefVersion",
    "MetricValue",
    "PipelineVersion",
    "PromptVersion",
]

#: ``pipeline_version`` 六元组的分隔符。
PIPELINE_VERSION_SEPARATOR = "|"


class EvidenceType(StrEnum):
    """证据类型。

    三类证据不可混淆（PRD §7.5 声明 1）：

    - ``OFFLINE_EVALUATION``：有金标集的离线评测，**可以**承诺「在该测试集上的准确率」。
    - ``OFFLINE_SIMULATION``：有历史日志的离线回放，**不可**承诺「线上 CTR 提升」。
    - ``ONLINE_GRAY`` / ``ONLINE_REAL``：真实流量，需要显著性与实验期说明。
    """

    OFFLINE_EVALUATION = "offline_evaluation"
    OFFLINE_SIMULATION = "offline_simulation"
    LOAD_TEST = "load_test"
    ENGINEERING_STAT = "engineering_stat"
    ONLINE_GRAY = "online_gray"
    ONLINE_REAL = "online_real"


@dataclass(frozen=True, slots=True)
class PipelineVersion(DomainModel):
    """流水线版本六元组。

    格式固定为 ``clean_rules|label_def|prompt|model|constraint|tag_space``，
    任一要素变更即变更（架构 §6.3）。
    """

    clean_rules_ver: str
    label_def_ver: str
    prompt_ver: str
    model_ver: str
    constraint_ver: str
    tag_space_ver: str

    def to_string(self) -> str:
        """渲染成落库的字符串形式。"""
        return PIPELINE_VERSION_SEPARATOR.join(
            (
                self.clean_rules_ver,
                self.label_def_ver,
                self.prompt_ver,
                self.model_ver,
                self.constraint_ver,
                self.tag_space_ver,
            )
        )

    @classmethod
    def parse(cls, raw: str) -> PipelineVersion:
        """从字符串解析。

        Args:
            raw: 六元组字符串。

        Returns:
            解析出的 :class:`PipelineVersion`。

        Raises:
            ValueError: 段数不是 6 时抛出——版本串格式错误必须显式失败，
                绝不能「解析出一半然后悄悄落库」。
        """
        parts = raw.split(PIPELINE_VERSION_SEPARATOR)
        if len(parts) != 6:
            raise ValueError(f"pipeline_version 必须是 6 段，实际得到 {len(parts)} 段：{raw!r}")
        return cls(
            clean_rules_ver=parts[0],
            label_def_ver=parts[1],
            prompt_ver=parts[2],
            model_ver=parts[3],
            constraint_ver=parts[4],
            tag_space_ver=parts[5],
        )

    def fingerprint(self) -> str:
        """返回六元组的短指纹（sha256 前 8 位）。

        用于「同一版本」判定，避免每次都做字符串比较。
        """
        return hashlib.sha256(self.to_string().encode("utf-8")).hexdigest()[:8]

    def is_compatible_with(self, other: PipelineVersion) -> bool:
        """判断两个版本的标签体系与特征空间是否一致。

        标签体系或特征空间不同 ⇒ 分数不可比（架构 §11.3 的 V5 校验）。
        """
        return (
            self.label_def_ver == other.label_def_ver and self.tag_space_ver == other.tag_space_ver
        )


@dataclass(frozen=True, slots=True)
class LabelDefVersion(DomainModel):
    """标签体系版本。"""

    version: str
    label_count: int = 0
    published_at: datetime | None = None
    active: bool = False
    deprecated: bool = False


@dataclass(frozen=True, slots=True)
class PromptVersion(DomainModel):
    """提示词版本组。

    system / user / arbiter / scoring 四套提示词**独立版本化**（架构 §14.7 的
    ``LLMRequest`` 契约要求 system 与 user 分开记版本）。
    """

    system: str
    user: str
    arbiter: str = ""
    scoring: str = ""

    def all_versions(self) -> tuple[str, ...]:
        """返回全部非空版本号。"""
        return tuple(v for v in (self.system, self.user, self.arbiter, self.scoring) if v)


@dataclass(frozen=True, slots=True)
class MetricValue(DomainModel):
    """一个对外展示的指标。

    **``evidence_type`` 没有默认值**——忘记填会在构造时直接 ``TypeError``，
    而不是等到报告里静默缺失（ADR-0010 决策 1）。这是刻意的摩擦。

    ``sample_size`` 与 ``as_of`` 同样必填：任何数字都必须能回答
    「分子是什么？分母是什么？数据从哪来？」（PRD §7.5 声明 3）。
    """

    name: str
    value: float
    evidence_type: EvidenceType
    sample_size: int
    as_of: date
    label_def_version: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None
    pipeline_version: str | None = None
    notes: str = ""

    def to_report_row(self) -> JsonObject:
        """渲染成报告表格的一行。

        字段顺序固定，方便 diff 两次运行的报告。
        """
        return {
            "name": self.name,
            "value": self.value,
            "evidence_type": str(self.evidence_type),
            "sample_size": self.sample_size,
            "as_of": self.as_of.isoformat(),
            "label_def_version": self.label_def_version,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "pipeline_version": self.pipeline_version,
            "notes": self.notes,
        }

    def requires_caveat(self) -> bool:
        """该指标是否必须附带「不可承诺线上效果」的说明。

        ``OFFLINE_SIMULATION`` 的结论**不可**被当作线上 CTR 提升来宣传。
        """
        return self.evidence_type == EvidenceType.OFFLINE_SIMULATION
