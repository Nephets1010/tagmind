"""标签树装载与结构测试。

对应 T03 验收项：

- ② 物化路径 ``path`` 与邻接表 ``parent`` 一致，``ancestors`` 数组正确
- ③ 无环、无孤儿（每个非 L1 节点 ``parent`` 必存在且 ``layer = parent.layer + 1``）
- ⑤ 祖先闭包校验在 1800 节点树上 < 50 ms
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tagmind.config.constants import (
    LAYER_COUNT,
    LAYER_QUOTAS,
    MAX_LABEL_TOTAL,
    MAX_SELECT_LIMIT,
    MIN_LABEL_TOTAL,
)
from tagmind.core.errors import ConstraintViolation
from tagmind.labeling.taxonomy import (
    load_taxonomy_from_yaml,
    measure_ancestor_closure,
    parse_taxonomy,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SEED_DIR = REPO_ROOT / "data" / "label_taxonomy"


@pytest.fixture(scope="module")
def tree():
    """装载 12 层标签种子。"""
    return load_taxonomy_from_yaml(SEED_DIR, version="ld_v1")


def test_seed_files_exist() -> None:
    """12 个层级文件必须齐全。"""
    for layer in range(1, LAYER_COUNT + 1):
        path = SEED_DIR / f"l{layer}.yaml"
        assert path.is_file(), f"缺少 {path}"


def test_total_in_range(tree) -> None:
    """标签总量必须落在 Q2 拍板的 1200–1800 区间。"""
    assert MIN_LABEL_TOTAL <= tree.total() <= MAX_LABEL_TOTAL, (
        f"标签总量 {tree.total()} 不在 {MIN_LABEL_TOTAL}–{MAX_LABEL_TOTAL} 区间内"
    )


def test_all_twelve_layers_populated(tree) -> None:
    """12 层都必须有标签。"""
    for layer in range(1, LAYER_COUNT + 1):
        assert tree.layer_count(layer) > 0, f"L{layer} 没有标签"


def test_layer_one_has_no_parent(tree) -> None:
    """L1 节点的 parent 必须为 NULL。"""
    for node in tree.by_layer(1):
        assert node.parent is None, f"{node.code} 是 L1，parent 必须为 NULL"


def test_parent_layer_is_exactly_one_less(tree) -> None:
    """每个非 L1 节点：parent 必存在，且 ``layer = parent.layer + 1``。"""
    for node in tree.nodes:
        if node.layer == 1:
            continue
        assert node.parent is not None, f"{node.code} 缺少 parent"
        parent = tree.by_code.get(node.parent)
        assert parent is not None, f"{node.code} 的父节点 {node.parent} 不存在（孤儿）"
        assert parent.layer == node.layer - 1, (
            f"{node.code}(L{node.layer}) 的父节点 {parent.code} 是 L{parent.layer}，"
            f"应为 L{node.layer - 1}"
        )


def test_no_cycles(tree) -> None:
    """无环：沿 parent 链上溯必须到达 L1 根节点。"""
    for node in tree.nodes:
        seen: set[str] = set()
        current = node
        while current.parent is not None:
            assert current.code not in seen, f"{node.code} 所在链路存在环"
            seen.add(current.code)
            parent = tree.by_code.get(current.parent)
            assert parent is not None
            current = parent


def test_path_matches_parent_chain(tree) -> None:
    """物化路径 ``path`` 与邻接表一致。"""
    for node in tree.nodes:
        expected = node.name
        if node.parent is not None:
            expected = f"{tree.by_code[node.parent].path}/{node.name}"
        assert node.path == expected, f"{node.code} 的 path 与 parent 链不一致"


def test_ancestors_array_correct(tree) -> None:
    """``ancestors`` 数组是 L1→L(k-1) 的有序 code 序列，长度恰为 layer-1。"""
    for node in tree.nodes:
        assert len(node.ancestors) == node.layer - 1, f"{node.code} 的 ancestors 长度错误"
        expected: list[str] = []
        current = node
        while current.parent is not None:
            expected.append(current.parent)
            parent = tree.by_code.get(current.parent)
            assert parent is not None
            current = parent
        expected.reverse()
        assert list(node.ancestors) == expected, f"{node.code} 的 ancestors 与 parent 链不一致"


def test_path_prefix(tree) -> None:
    """``path_prefix`` 是父节点的完整路径 + ``/``。"""
    for node in tree.nodes:
        if node.parent is None:
            assert node.path_prefix == "", f"L1 节点 {node.code} 的 path_prefix 应为空"
            continue
        assert node.path_prefix == f"{tree.by_code[node.parent].path}/", (
            f"{node.code} 的 path_prefix 与父节点路径不一致"
        )


def test_root_code(tree) -> None:
    """``root_code`` 是所属的 L1 节点 code。"""
    for node in tree.nodes:
        if node.layer == 1:
            assert node.root_code == node.code
        else:
            assert node.root_code == tree.by_code[node.parent].root_code


def test_depth_equals_layer(tree) -> None:
    """``depth`` 必须等于 ``layer``。"""
    for node in tree.nodes:
        assert node.depth == node.layer


def test_quota_respected(tree) -> None:
    """每个标签的 ``max_select`` 不得超过该层配额，且不超过 8。"""
    for node in tree.nodes:
        assert node.max_select <= MAX_SELECT_LIMIT
        assert node.max_select <= LAYER_QUOTAS[node.layer], (
            f"{node.code} 的 max_select={node.max_select} 超过 L{node.layer} 配额 "
            f"{LAYER_QUOTAS[node.layer]}"
        )


def test_l1_l2_quota_is_one(tree) -> None:
    """L1 与 L2 必须严格单选（配额恒为 1）。"""
    for layer in (1, 2):
        for node in tree.by_layer(layer):
            assert node.max_select == 1, f"{node.code} 的 max_select 必须为 1"


def test_validate_release_passes(tree) -> None:
    """发布期校验（A1–A10）必须零违规。"""
    violations = tree.validate_release()
    assert not violations, "发布期校验失败：\n" + "\n".join(violations)


def test_ancestor_closure_performance(tree) -> None:
    """祖先闭包校验在 1800 节点树上必须 < 50 ms。"""
    codes = [node.code for node in tree.nodes]
    assert len(codes) >= MIN_LABEL_TOTAL
    elapsed = measure_ancestor_closure(tree, codes)
    assert elapsed < 50.0, f"祖先闭包校验耗时 {elapsed:.2f} ms，超过 50 ms 上限"


def test_tree_lookup_helpers(tree) -> None:
    """树的查询助手必须与邻接表一致。"""
    node = tree.by_layer(4)[0]
    assert tree.children_of(node.code) == tuple(
        tree.by_code[child] for child in tree.children.get(node.code, ())
    )
    assert tree.ancestors_of(node.code) == node.ancestors
    assert tree.path_of(node.code) == node.path
    siblings = tree.siblings_of(node.code)
    assert node.code in siblings
    # 兄弟集合 = 同一父节点的全部子节点（含自身）。
    assert set(siblings) == set(tree.children.get(node.parent, ()))
    assert len(siblings) == len(tree.children_of(node.parent))


def test_to_label_set_roundtrip(tree) -> None:
    """转成领域层 ``LabelSet`` 后信息不丢失。"""
    label_set = tree.to_label_set()
    assert label_set.total() == tree.total()
    assert label_set.version == tree.version
    assert len(label_set.roots) == tree.layer_count(1)


def test_to_tree_payload(tree) -> None:
    """嵌套树结构可序列化（API 层直接返回该结构）。"""
    payload = tree.to_label_set().to_tree_payload()
    assert payload["version"] == tree.version
    assert len(payload["roots"]) == tree.layer_count(1)
    assert payload["roots"][0]["layer"] == 1
    assert "children" in payload["roots"][0]


def test_duplicate_code_rejected() -> None:
    """重复 code 必须抛 ``ConstraintViolation``。"""
    raw = {1: [{"code": "X", "name": "甲"}, {"code": "X", "name": "乙"}]}
    with pytest.raises(ConstraintViolation):
        parse_taxonomy(raw)


def test_missing_parent_rejected() -> None:
    """父节点不存在必须抛 ``ConstraintViolation``。"""
    raw = {1: [{"code": "A", "name": "甲"}], 2: [{"code": "B", "name": "乙", "parent": "Z"}]}
    with pytest.raises(ConstraintViolation):
        parse_taxonomy(raw)


def test_cycle_rejected() -> None:
    """成环必须抛 ``ConstraintViolation``。"""
    raw = {
        1: [{"code": "A", "name": "甲"}],
        2: [{"code": "B", "name": "乙", "parent": "C"}],
        3: [{"code": "C", "name": "丙", "parent": "B"}],
    }
    with pytest.raises(ConstraintViolation):
        parse_taxonomy(raw)


def test_label_names_are_real_chinese(tree) -> None:
    """标签名必须是真实中文，不允许 ``L1_TAG_001`` 这类占位符。"""
    import re

    placeholder = re.compile(r"^(L\d+_)?(TAG|LABEL|PLACEHOLDER|XXX|TODO)[_\-]?\d*$", re.IGNORECASE)
    offenders = [node.code for node in tree.nodes if placeholder.match(node.name)]
    assert not offenders, f"发现占位符标签名：{offenders[:10]}"


def test_every_label_has_description(tree) -> None:
    """每个标签必须有非空 description（它要进 system prompt）。"""
    for node in tree.nodes:
        assert node.description.strip(), f"{node.code} 缺少 description"


def test_every_label_has_name(tree) -> None:
    """每个标签必须有非空 name。"""
    for node in tree.nodes:
        assert node.name.strip(), f"{node.code} 缺少 name"
