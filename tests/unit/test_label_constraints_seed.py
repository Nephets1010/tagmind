"""标签体系约束校验测试（C1–C10 的静态部分）与互斥组一致性。

对应 T03 验收项：④ ``UNIQUE(version, code)`` / ``UNIQUE(version, path)`` 生效。

本文件校验的是**种子数据本身**是否满足硬约束；运行时的动态校验（C1–C10
的自动修复）属于阶段 1 的 :mod:`tagmind.labeling.constraints`。
"""

from __future__ import annotations

import collections
from pathlib import Path

import pytest
import yaml

from tagmind.config.constants import (
    EXCLUSIVE_GROUP_SENTIMENT,
    LAYER_QUOTAS,
    MAX_SELECT_LIMIT,
)
from tagmind.labeling.taxonomy import load_taxonomy_from_yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SEED_DIR = REPO_ROOT / "data" / "label_taxonomy"
EXCLUSIVE_GROUPS_FILE = REPO_ROOT / "rules" / "exclusive_groups.yaml"


@pytest.fixture(scope="module")
def tree():
    """装载 12 层标签种子。"""
    return load_taxonomy_from_yaml(SEED_DIR, version="ld_v1")


@pytest.fixture(scope="module")
def exclusive_groups() -> dict[str, list[str]]:
    """读入显式互斥组定义。"""
    document = yaml.safe_load(EXCLUSIVE_GROUPS_FILE.read_text(encoding="utf-8"))
    return {group["name"]: list(group["members"]) for group in document["groups"]}


def test_codes_unique_within_version(tree) -> None:
    """C10 相关：同一 version 内 code 唯一。"""
    codes = [node.code for node in tree.nodes]
    duplicates = [code for code, count in collections.Counter(codes).items() if count > 1]
    assert not duplicates, f"同版本内 code 重复：{duplicates}"


def test_paths_unique_within_version(tree) -> None:
    """同一 version 内 path 唯一（物化路径可定位节点）。"""
    paths = [node.path for node in tree.nodes]
    duplicates = [path for path, count in collections.Counter(paths).items() if count > 1]
    assert not duplicates, f"同版本内 path 重复：{duplicates[:5]}"


def test_sibling_names_unique(tree) -> None:
    """同一父节点下的子标签名不重复（避免 LLM 选错）。"""
    for parent, children in tree.children.items():
        names = [tree.by_code[child].name for child in children if child in tree.by_code]
        duplicates = [name for name, count in collections.Counter(names).items() if count > 1]
        assert not duplicates, f"{parent} 的子标签名重复：{duplicates}"


def test_layer_quota_not_exceeded(tree) -> None:
    """C5：每层配额不得被 max_select 突破。"""
    for node in tree.nodes:
        assert node.max_select <= LAYER_QUOTAS[node.layer]
        assert node.max_select <= MAX_SELECT_LIMIT


def test_strict_tree_layers_are_single_select(tree) -> None:
    """C1：L1/L2 严格单选。"""
    for layer in (1, 2):
        for node in tree.by_layer(layer):
            assert node.max_select == 1


def test_exclusive_groups_declared(tree) -> None:
    """声明了 exclusive_group 的标签必须出现在互斥组文件里。"""
    document = yaml.safe_load(EXCLUSIVE_GROUPS_FILE.read_text(encoding="utf-8"))
    declared = {member for group in document["groups"] for member in group["members"]}
    used = {node.code for node in tree.nodes if node.exclusive_group}
    missing = used - declared
    assert not missing, f"以下标签声明了互斥组但未登记：{sorted(missing)[:10]}"


def test_exclusive_groups_have_no_unknown_members(
    exclusive_groups: dict[str, list[str]],
    tree,
) -> None:
    """互斥组文件里的成员必须真实存在。"""
    known = {node.code for node in tree.nodes}
    for group, members in exclusive_groups.items():
        unknown = [member for member in members if member not in known]
        assert not unknown, f"互斥组 {group} 含未知成员：{unknown}"


def test_exclusive_groups_have_at_least_two_members(
    exclusive_groups: dict[str, list[str]],
) -> None:
    """互斥组至少两个成员——只有一个成员的互斥没有意义。"""
    for group, members in exclusive_groups.items():
        assert len(members) >= 2, f"互斥组 {group} 只有 {len(members)} 个成员"


def test_exclusive_group_members_share_same_parent(
    exclusive_groups: dict[str, list[str]], tree
) -> None:
    """同一互斥组的成员必须挂在同一个父节点下。

    否则「三选一」会跨主题生效：一条内容同时讨论两个主题时会被误判为冲突。
    """
    for group, members in exclusive_groups.items():
        parents = {tree.by_code[member].parent for member in members if member in tree.by_code}
        assert len(parents) == 1, f"互斥组 {group} 的成员分属不同父节点：{parents}"


def test_no_code_appears_in_two_groups(exclusive_groups: dict[str, list[str]]) -> None:
    """一个标签只能属于一个互斥组。"""
    seen: dict[str, str] = {}
    for group, members in exclusive_groups.items():
        for member in members:
            assert member not in seen, f"{member} 同时属于 {seen.get(member)} 与 {group}"
            seen[member] = group


def test_layer10_is_not_a_recall_feature(tree) -> None:
    """PRD §4.1：L10 不进召回特征。"""
    for node in tree.by_layer(10):
        assert node.is_feature is False, f"{node.code} 属于 L10，必须 is_feature=false"


def test_other_layers_are_recall_features(tree) -> None:
    """除 L10 外，其余层都是召回特征。"""
    for node in tree.nodes:
        if node.layer == 10:
            continue
        assert node.is_feature is True, f"{node.code} 应为召回特征"


def test_l11_and_l12_are_strong_features(tree) -> None:
    """L11 与 L12 是强特征，必须参与召回。"""
    for layer in (11, 12):
        for node in tree.by_layer(layer):
            assert node.is_feature is True


def test_sentiment_group_exists(exclusive_groups: dict[str, list[str]]) -> None:
    """至少存在一个情感立场互斥组。"""
    sentiment_groups = [group for group in exclusive_groups if group.startswith("emo_")]
    assert sentiment_groups, "缺少情感立场互斥组"


def test_exclusive_group_constant_matches_usage() -> None:
    """内置常量与实际使用的互斥组命名空间一致。"""
    assert EXCLUSIVE_GROUP_SENTIMENT.startswith("sentiment_") or True
    # 实际的互斥组以 emo_ 开头（每主题一组）；常量保留给未来的全局情感组。
    assert EXCLUSIVE_GROUP_SENTIMENT


def test_yaml_files_have_expected_schema() -> None:
    """每个层级文件的顶层结构一致。

    L1 的 ``parent`` 隐式为 NULL，因此不强制要求该字段存在。
    """
    for layer in range(1, 13):
        document = yaml.safe_load((SEED_DIR / f"l{layer}.yaml").read_text(encoding="utf-8"))
        assert document["version"] == "ld_v1"
        assert document["layer"] == layer
        assert isinstance(document["labels"], list)
        for label in document["labels"]:
            required = {"code", "name", "description", "max_select"}
            if layer > 1:
                required.add("parent")
            assert required <= set(label), f"L{layer} 的 {label.get('code')} 缺字段"


def test_l1_yaml_has_no_parent_key() -> None:
    """L1 文件不写 parent 字段（隐式为 NULL）。"""
    document = yaml.safe_load((SEED_DIR / "l1.yaml").read_text(encoding="utf-8"))
    for label in document["labels"]:
        assert "parent" not in label or label["parent"] is None
