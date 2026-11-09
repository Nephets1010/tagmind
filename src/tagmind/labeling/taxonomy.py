"""12 层标签树装载：邻接表 + 物化路径 + 祖先闭包。

对应架构文档 §10.1 与 ADR-0008。

**三写结构**：

- ``parent``（邻接表）是**真相源**——树的定义。
- ``path``（物化路径）是派生索引——一次查询取全路径，字符串前缀即子树。
- ``ancestors``（祖先数组）是派生索引——C3 祖先闭包校验退化为 O(1) 判定。

**为什么三者并存**：仅邻接表 → 祖先回溯需递归查询（N 次 IO），C3 校验在批量
打标时成为瓶颈；仅物化路径 → 同层兄弟互斥（C2）判断要拆路径，逻辑易错。
三者组合后 C1/C2/C3 的校验全部是**单表 O(1) 判定**，符合「80% 约束用规则
解决」的成本原则。

**发布期校验**（ADR-0008 的 A1–A10）：任一断言失败即拒绝发布，
抛 :class:`ConstraintViolation`。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

from tagmind.config.constants import (
    LAYER_COUNT,
    LAYER_QUOTAS,
    MAX_LABEL_TOTAL,
    MAX_SELECT_LIMIT,
    MIN_LABEL_TOTAL,
)
from tagmind.core.errors import ConstraintViolation
from tagmind.domain.label import LabelDef, LabelNode, LabelSet

__all__ = [
    "TaxonomyTree",
    "load_taxonomy_files",
    "load_taxonomy_from_yaml",
    "parse_taxonomy",
]

#: 种子目录中每层的文件名模板。
_LAYER_FILE_TEMPLATE: Final[str] = "l{layer}.yaml"

#: 标签定义的 YAML 字段名 → dataclass 字段名。
_FIELD_MAP: Final[dict[str, str]] = {
    "code": "code",
    "name": "name",
    "description": "description",
    "parent": "parent",
    "max_select": "max_select",
    "exclusive_group": "exclusive_group",
    "sibling_exclusion": "sibling_exclusion",
    "examples": "examples",
    "is_feature": "is_feature",
    "status": "status",
}


@dataclass(frozen=True, slots=True)
class TaxonomyTree:
    """装载好的标签树（发布前快照）。

    Attributes:
        version: 标签体系版本。
        nodes: 全部节点。
        by_code: code → 节点。
        children: code → 子节点 code 列表。
        roots: L1 节点的 code 列表。
        layer_nodes: 层号 → 该层 code 列表。
    """

    version: str
    nodes: tuple[LabelNode, ...] = ()
    by_code: dict[str, LabelNode] = field(default_factory=dict, compare=False, repr=False)
    children: dict[str, tuple[str, ...]] = field(default_factory=dict, compare=False, repr=False)
    roots: tuple[str, ...] = ()
    layer_nodes: dict[int, tuple[str, ...]] = field(default_factory=dict, compare=False, repr=False)

    def total(self) -> int:
        """标签总数。"""
        return len(self.nodes)

    def by_layer(self, layer: int) -> tuple[LabelNode, ...]:
        """取某层的全部节点。"""
        return tuple(self.by_code[code] for code in self.layer_nodes.get(layer, ()))

    def layer_count(self, layer: int) -> int:
        """某层的标签数量。"""
        return len(self.layer_nodes.get(layer, ()))

    def ancestors_of(self, code: str) -> tuple[str, ...]:
        """取某节点的祖先 code 序列（L1 → L(k-1)）。"""
        node = self.by_code.get(code)
        return node.ancestors if node else ()

    def children_of(self, code: str) -> tuple[LabelNode, ...]:
        """取某节点的直接子节点。"""
        return tuple(
            self.by_code[child] for child in self.children.get(code, ()) if child in self.by_code
        )

    def siblings_of(self, code: str) -> tuple[str, ...]:
        """取某节点的兄弟 code 序列（含自身）。"""
        node = self.by_code.get(code)
        if node is None:
            return ()
        if node.parent is None:
            return (node.code,)
        return self.children.get(node.parent, (code,))

    def path_of(self, code: str) -> str:
        """取某节点的物化路径。"""
        node = self.by_code.get(code)
        return node.path if node else ""

    def to_label_set(self) -> LabelSet:
        """转成领域层的 :class:`LabelSet` 快照。"""
        return LabelSet(
            version=self.version,
            nodes=self.nodes,
            by_code=self.by_code,
            children=self.children,
            roots=self.roots,
            layer_nodes=self.layer_nodes,
        )

    # --- 发布期校验（ADR-0008 的 A1–A10） ---------------------------

    def validate_release(self) -> list[str]:
        """执行发布期校验。

        Returns:
            违规说明列表；为空表示校验通过。**不抛异常**——调用方决定如何
            呈现（CLI 打印列表、CI 阻断）。
        """
        violations: list[str] = []

        violations.extend(self._validate_structure())
        violations.extend(self._validate_paths())
        violations.extend(self._validate_quotas())
        violations.extend(self._validate_exclusive_groups())
        violations.extend(self._validate_total())
        return violations

    def _validate_structure(self) -> list[str]:
        """A1/A2/A3：层与父的关系、无环、深度。"""
        violations: list[str] = []
        for node in self.nodes:
            if node.layer == 1:
                if node.parent is not None:
                    violations.append(f"A1 {node.code}：L1 节点的 parent 必须为 NULL")
                continue
            if node.parent is None:
                violations.append(f"A2 {node.code}：L{node.layer} 节点缺少 parent")
                continue
            parent = self.by_code.get(node.parent)
            if parent is None:
                violations.append(f"A2 {node.code}：父节点 {node.parent} 不存在（孤儿）")
            elif parent.layer != node.layer - 1:
                violations.append(
                    f"A2 {node.code}：父节点 {parent.code} 的层是 L{parent.layer}，"
                    f"应为 L{node.layer - 1}"
                )
        # A3 无环：沿 parent 链上溯，步数不得超过层数。
        for node in self.nodes:
            seen: set[str] = set()
            current = node
            steps = 0
            while current.parent is not None:
                if current.code in seen:
                    violations.append(f"A3 {node.code}：检测到环")
                    break
                seen.add(current.code)
                parent = self.by_code.get(current.parent)
                if parent is None:
                    break
                current = parent
                steps += 1
                if steps > LAYER_COUNT:
                    violations.append(f"A3 {node.code}：祖先链超过 {LAYER_COUNT} 层")
                    break
        return violations

    def _validate_paths(self) -> list[str]:
        """A4/A5/A6/A7：物化路径、祖先数组、路径前缀、根 code。"""
        violations: list[str] = []
        for node in self.nodes:
            expected_path, expected_ancestors, expected_prefix, expected_root = self._derive(node)
            if node.path != expected_path:
                violations.append(
                    f"A4 {node.code}：path 与 parent 链不一致"
                    f"（期望 {expected_path!r}，实际 {node.path!r}）"
                )
            if node.ancestors != expected_ancestors:
                violations.append(f"A5 {node.code}：ancestors 与 parent 链不一致")
            if node.path_prefix != expected_prefix:
                violations.append(f"A6 {node.code}：path_prefix 与 path 不一致")
            if node.root_code != expected_root:
                violations.append(f"A7 {node.code}：root_code 与 L1 祖先不一致")
            if node.depth != node.layer:
                violations.append(f"A3 {node.code}：depth 与 layer 不一致")
        return violations

    def _derive(self, node: LabelNode) -> tuple[str, tuple[str, ...], str, str]:
        """从 parent 链推导 path / ancestors / path_prefix / root_code。

        Args:
            node: 目标节点。

        Returns:
            ``(path, ancestors, path_prefix, root_code)``。
        """
        chain: list[LabelNode] = []
        current: LabelNode | None = node
        while current is not None and current.parent is not None:
            parent = self.by_code.get(current.parent)
            if parent is None:
                break
            chain.append(parent)
            current = parent
        chain.reverse()

        names = [item.name for item in chain] + [node.name]
        path = "/".join(names)
        ancestors = tuple(item.code for item in chain)
        prefix = ("/".join(names[:-1]) + "/") if len(names) > 1 else ""
        root = chain[0].code if chain else node.code
        return path, ancestors, prefix, root

    def _validate_quotas(self) -> list[str]:
        """A9：配额合法。"""
        violations: list[str] = []
        for node in self.nodes:
            if node.max_select < 0:
                violations.append(f"A9 {node.code}：max_select 不能为负")
            if node.max_select > MAX_SELECT_LIMIT:
                violations.append(
                    f"A9 {node.code}：max_select={node.max_select} 超过上限 {MAX_SELECT_LIMIT}"
                )
            expected = LAYER_QUOTAS.get(node.layer)
            if expected is not None and node.max_select > expected:
                violations.append(
                    f"A9 {node.code}：max_select={node.max_select} 超过 L{node.layer} 的配额 {expected}"
                )
            if node.layer in (1, 2) and node.max_select != 1:
                violations.append(f"A9 {node.code}：L{node.layer} 的 max_select 必须为 1")
        return violations

    def _validate_exclusive_groups(self) -> list[str]:
        """A10：互斥组内标签集合非空且不含自身。"""
        violations: list[str] = []
        groups: dict[str, list[str]] = {}
        for node in self.nodes:
            if node.exclusive_group:
                groups.setdefault(node.exclusive_group, []).append(node.code)
        for group, codes in groups.items():
            if len(codes) < 2:
                violations.append(f"A10 互斥组 {group} 只有一个标签，互斥无意义：{codes}")
            if len(set(codes)) != len(codes):
                violations.append(f"A10 互斥组 {group} 内存在重复标签")
        return violations

    def _validate_total(self) -> list[str]:
        """A8：总量落在 Q2 拍板的 1200–1800 区间。"""
        total = self.total()
        if not MIN_LABEL_TOTAL <= total <= MAX_LABEL_TOTAL:
            return [f"A8 标签总量 {total} 不在 {MIN_LABEL_TOTAL}–{MAX_LABEL_TOTAL} 区间内"]
        return []

    # --- 派生结构 -----------------------------------------------------

    def to_label_defs(self, *, label_ids: dict[str, int] | None = None) -> list[LabelDef]:
        """转成可落库的 :class:`LabelDef` 列表。

        Args:
            label_ids: code → 已分配的 label_id；``None`` 时用 0 占位
                （由数据库 identity 分配）。

        Returns:
            标签定义列表。
        """
        definitions: list[LabelDef] = []
        for node in self.nodes:
            definitions.append(
                LabelDef(
                    id=(label_ids or {}).get(node.code, 0),
                    code=node.code,
                    layer=node.layer,
                    name=node.name,
                    description=node.description,
                    examples=node.examples,
                    parent=node.parent,
                    max_select=node.max_select,
                    exclusive_group=node.exclusive_group,
                    sibling_exclusion=node.sibling_exclusion,
                    is_feature=node.is_feature,
                    status="active",
                    version=self.version,
                    path=node.path,
                    path_text=node.path.lower(),
                    depth=node.depth,
                    ancestors=node.ancestors,
                    root_code=node.root_code,
                    df_count=0,
                    idf=None,
                    path_prefix=node.path_prefix,
                )
            )
        return definitions


def parse_taxonomy(
    raw_layers: dict[int, list[dict[str, Any]]],
    *,
    version: str = "ld_v1",
) -> TaxonomyTree:
    """从已解析的 YAML 数据构建标签树。

    Args:
        raw_layers: 层号 → 该层的标签定义字典列表。
        version: 标签体系版本。

    Returns:
        装载好的 :class:`TaxonomyTree`。

    Raises:
        ConstraintViolation: 存在重复 code 或缺失父节点时抛出。
    """
    by_code: dict[str, LabelNode] = {}
    raw_by_code: dict[str, dict[str, Any]] = {}

    for layer in sorted(raw_layers):
        for entry in raw_layers[layer]:
            code = str(entry.get("code", "")).strip()
            if not code:
                raise ConstraintViolation(
                    "标签定义缺少 code", details={"layer": layer, "entry": entry}
                )
            if code in raw_by_code:
                raise ConstraintViolation(
                    "标签 code 重复",
                    details={
                        "code": code,
                        "layer": layer,
                        "first_layer": raw_by_code[code].get("layer"),
                    },
                )
            enriched = dict(entry)
            enriched["layer"] = layer
            raw_by_code[code] = enriched

    # 第一遍：建节点（path 等派生字段先留空）。
    for code, entry in raw_by_code.items():
        by_code[code] = LabelNode(
            code=code,
            layer=int(entry["layer"]),
            name=str(entry.get("name", code)),
            description=str(entry.get("description", "")),
            parent=entry.get("parent"),
            path="",
            ancestors=(),
            children=(),
            root_code="",
            max_select=int(entry.get("max_select", LAYER_QUOTAS.get(int(entry["layer"]), 1))),
            exclusive_group=entry.get("exclusive_group"),
            sibling_exclusion=tuple(entry.get("sibling_exclusion", ()) or ()),
            is_feature=bool(entry.get("is_feature", True)),
            examples=tuple(entry.get("examples", ()) or ()),
            label_id=int(entry.get("label_id", 0)) or None,
            depth=int(entry["layer"]),
        )

    # 第二遍：建父子关系。
    children: dict[str, list[str]] = {}
    roots: list[str] = []
    layer_nodes: dict[int, list[str]] = {}
    for code, entry in raw_by_code.items():
        layer = int(entry["layer"])
        layer_nodes.setdefault(layer, []).append(code)
        parent = entry.get("parent")
        if parent is None:
            roots.append(code)
        else:
            if parent not in by_code:
                raise ConstraintViolation(
                    "标签的父节点不存在",
                    details={"code": code, "parent": parent},
                )
            children.setdefault(parent, []).append(code)

    # 第三遍：回填派生的 path / ancestors / path_prefix / root_code。
    resolved: dict[str, LabelNode] = {}

    def _resolve(code: str, stack: tuple[str, ...] = ()) -> LabelNode:
        if code in resolved:
            return resolved[code]
        if code in stack:
            raise ConstraintViolation("标签树存在环", details={"code": code, "stack": list(stack)})
        node = by_code[code]
        if node.parent is None:
            path = node.name
            ancestors: tuple[str, ...] = ()
            prefix = ""
            root = node.code
        else:
            parent_node = _resolve(node.parent, (*stack, code))
            path = f"{parent_node.path}/{node.name}"
            ancestors = (*parent_node.ancestors, parent_node.code)
            prefix = f"{parent_node.path}/"
            root = parent_node.root_code
        updated = LabelNode(
            code=node.code,
            layer=node.layer,
            name=node.name,
            description=node.description,
            parent=node.parent,
            path=path,
            path_prefix=prefix,
            ancestors=ancestors,
            children=tuple(children.get(code, ())),
            root_code=root,
            max_select=node.max_select,
            exclusive_group=node.exclusive_group,
            sibling_exclusion=node.sibling_exclusion,
            is_feature=node.is_feature,
            examples=node.examples,
            label_id=node.label_id,
            depth=node.layer,
        )
        resolved[code] = updated
        return updated

    nodes = tuple(_resolve(code) for code in raw_by_code)

    return TaxonomyTree(
        version=version,
        nodes=nodes,
        by_code={node.code: node for node in nodes},
        children={key: tuple(value) for key, value in children.items()},
        roots=tuple(sorted(roots)),
        layer_nodes={key: tuple(value) for key, value in layer_nodes.items()},
    )


def load_taxonomy_files(paths: dict[int, Path], *, version: str = "ld_v1") -> TaxonomyTree:
    """从 YAML 文件装载标签树。

    Args:
        paths: 层号 → 文件路径。
        version: 标签体系版本。

    Returns:
        装载好的 :class:`TaxonomyTree`。
    """
    raw_layers: dict[int, list[dict[str, Any]]] = {}
    for layer, path in sorted(paths.items()):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        labels = document.get("labels") or []
        raw_layers[layer] = list(labels)
    return parse_taxonomy(raw_layers, version=version)


def load_taxonomy_from_yaml(directory: str | Path, *, version: str = "ld_v1") -> TaxonomyTree:
    """从目录装载标签树。

    目录中应存在 ``l1.yaml`` ~ ``l12.yaml``；缺失的层视为空层。

    Args:
        directory: 种子目录。
        version: 标签体系版本。

    Returns:
        装载好的 :class:`TaxonomyTree`。
    """
    root = Path(directory)
    paths = {
        layer: root / _LAYER_FILE_TEMPLATE.format(layer=layer)
        for layer in range(1, LAYER_COUNT + 1)
        if (root / _LAYER_FILE_TEMPLATE.format(layer=layer)).is_file()
    }
    return load_taxonomy_files(paths, version=version)


def measure_ancestor_closure(tree: TaxonomyTree, codes: list[str]) -> float:
    """测量祖先闭包校验的耗时。

    对应 T03 的验收项「祖先闭包校验在 1800 节点树上 < 50 ms」。

    Args:
        tree: 标签树。
        codes: 待校验的标签 code 列表。

    Returns:
        耗时（毫秒）。
    """
    start = time.perf_counter()
    for code in codes:
        node = tree.by_code.get(code)
        if node is None:
            continue
        _ = set(node.ancestors)
    return (time.perf_counter() - start) * 1000
