"""标签领域模型：LabelDef / LabelNode / LabelSet / LabelCandidate。

对应 ``label_def`` 表（架构文档 §5.3）与 12 层树的进程内表示。

三写结构（ADR-0008）：邻接表 ``parent`` 是**真相源**，``path`` 与 ``ancestors``
是派生索引。三者并存让 C1/C2/C3 的校验全部退化为单表 O(1) 判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from tagmind.domain.base import DomainModel, JsonObject

__all__ = [
    "ConflictType",
    "LabelCandidate",
    "LabelDef",
    "LabelNode",
    "LabelSet",
    "LabelSource",
]


class LabelSource(StrEnum):
    """标签来源。

    ``DERIVED`` 表示由祖先闭包规则（C3）自动补齐的祖先标签——它**不占用**
    该层配额，也不参与标签向量计算（架构 §11.4）。
    """

    LLM = "llm"
    RULE = "rule"
    RAG = "rag"
    DERIVED = "derived"
    MANUAL = "manual"


class ConflictType(StrEnum):
    """冲突类型。

    取值域与 ``conflict_type_t`` 域一致。前三类由确定性规则检出（零 LLM 成本），
    第四类需要 RAG 仲裁。
    """

    PARENT = "parent"
    MUTUAL = "mutual"
    EXCLUSIVE = "exclusive"
    SEMANTIC_CANDIDATE = "semantic_candidate"


@dataclass(frozen=True, slots=True)
class LabelDef(DomainModel):
    """标签定义（数据库行形态）。"""

    id: int
    code: str
    layer: int
    name: str
    description: str = ""
    examples: tuple[str, ...] = ()
    parent: str | None = None
    max_select: int = 1
    exclusive_group: str | None = None
    sibling_exclusion: tuple[str, ...] = ()
    is_feature: bool = True
    status: str = "active"
    version: str = ""
    path: str = ""
    path_text: str = ""
    depth: int = 0
    ancestors: tuple[str, ...] = ()
    root_code: str = ""
    df_count: int = 0
    idf: float | None = None
    path_prefix: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class LabelNode(DomainModel):
    """标签树节点（进程内形态）。

    ``children`` 存子节点的 code 列表而非对象引用，避免递归结构在序列化时
    无限展开。
    """

    code: str
    layer: int
    name: str
    description: str = ""
    parent: str | None = None
    path: str = ""
    path_prefix: str = ""
    ancestors: tuple[str, ...] = ()
    children: tuple[str, ...] = ()
    root_code: str = ""
    max_select: int = 1
    exclusive_group: str | None = None
    sibling_exclusion: tuple[str, ...] = ()
    is_feature: bool = True
    examples: tuple[str, ...] = ()
    label_id: int | None = None
    df_count: int = 0
    idf: float | None = None
    depth: int = 0

    def is_ancestor_of(self, other: LabelNode) -> bool:
        """判断本节点是否为 ``other`` 的祖先。"""
        return self.code in other.ancestors

    def depth_from_root(self) -> int:
        """从 L1 起算的深度（根节点为 1）。"""
        return len(self.ancestors) + 1


@dataclass(frozen=True, slots=True)
class LabelSet(DomainModel):
    """一个版本的完整标签树快照。

    ``taxonomy_repo.load_tree(version)`` 返回该对象；它是**只读快照**，
    进程内缓存（≤1800 行，一次查询取全量）。
    """

    version: str
    nodes: tuple[LabelNode, ...] = ()
    by_code: dict[str, LabelNode] = field(default_factory=dict, compare=False, repr=False)
    children: dict[str, tuple[str, ...]] = field(default_factory=dict, compare=False, repr=False)
    roots: tuple[str, ...] = ()
    layer_nodes: dict[int, tuple[str, ...]] = field(default_factory=dict, compare=False, repr=False)

    def node(self, code: str) -> LabelNode | None:
        """按 code 取节点。"""
        return self.by_code.get(code)

    def children_of(self, code: str) -> tuple[LabelNode, ...]:
        """取某节点的直接子节点。"""
        return tuple(
            self.by_code[child] for child in self.children.get(code, ()) if child in self.by_code
        )

    def ancestors_of(self, code: str) -> tuple[str, ...]:
        """取某节点的祖先 code 序列（L1 → L(k-1)）。"""
        node = self.by_code.get(code)
        return node.ancestors if node else ()

    def siblings_of(self, code: str) -> tuple[str, ...]:
        """取某节点的兄弟 code 序列（含自身）。"""
        node = self.by_code.get(code)
        if node is None:
            return ()
        if node.parent is None:
            return (node.code,)
        return self.children.get(node.parent, (code,))

    def by_layer(self, layer: int) -> tuple[LabelNode, ...]:
        """取某层的全部节点。"""
        return tuple(self.by_code[code] for code in self.layer_nodes.get(layer, ()))

    def path_of(self, code: str) -> str:
        """取某节点的物化路径。"""
        node = self.by_code.get(code)
        return node.path if node else ""

    def total(self) -> int:
        """标签总数。"""
        return len(self.nodes)

    def exclusive_groups(self) -> dict[str, tuple[str, ...]]:
        """按互斥组聚合标签 code。

        Returns:
            组名 → 该组内全部标签 code。
        """
        groups: dict[str, list[str]] = {}
        for node in self.nodes:
            if node.exclusive_group:
                groups.setdefault(node.exclusive_group, []).append(node.code)
        return {group: tuple(codes) for group, codes in groups.items()}

    def to_tree_payload(self) -> JsonObject:
        """导出嵌套树结构（API 层直接返回该结构）。"""

        def _build(code: str) -> JsonObject:
            node = self.by_code[code]
            return {
                "code": node.code,
                "name": node.name,
                "layer": node.layer,
                "label_id": node.label_id,
                "max_select": node.max_select,
                "exclusive_group": node.exclusive_group,
                "is_feature": node.is_feature,
                "children": [_build(child) for child in self.children.get(code, ())],
            }

        return {"version": self.version, "roots": [_build(code) for code in self.roots]}


@dataclass(frozen=True, slots=True)
class LabelCandidate(DomainModel):
    """标签候选（打标输出的最小单元）。

    ``confidence`` 是置信度，低于 C7 门槛（0.7）的候选不进 ``labels``，
    但保留在 ``label_raw``。
    """

    label_id: int
    code: str
    layer: int
    confidence: float
    source: LabelSource = LabelSource.RULE
    def_version: str = ""
    name: str = ""

    @property
    def is_derived(self) -> bool:
        """是否由祖先闭包自动补齐。"""
        return self.source == LabelSource.DERIVED

    def to_payload(self) -> JsonObject:
        """转成 ``content.labels`` JSONB 的元素结构。"""
        return {
            "label_id": self.label_id,
            "code": self.code,
            "layer": self.layer,
            "name": self.name,
            "confidence": self.confidence,
            "source": str(self.source),
            "def_version": self.def_version,
        }
