"""阈值与提示词模板的契约测试。

这两个模块承载了 ADR-0010（口径诚实）与 ADR-0002（分片装载）的核心约束：

* ``thresholds.py`` 里的数字会同时影响「标签向量」「质量闸门」「冲突检测」
  「画像衰减」四条链路，改动必须让版本串变化，否则新旧分数会被混算；
* ``prompt_templates.py`` 的输出 schema 是解析器与校验器的契约，改动会让
  历史审计记录无法回放。
"""

from __future__ import annotations

import json

import pytest

from tagmind.config import prompt_templates as pt
from tagmind.config import thresholds as th


class TestThresholdInvariants:
    """阈值之间的相对关系必须自洽。"""

    def test_quality_score_ordering(self) -> None:
        """拒绝线 < 复核线 < 发布线。"""
        assert th.QUALITY_REJECT_SCORE < th.QUALITY_REVIEW_SCORE < th.QUALITY_MIN_SCORE

    def test_quality_scores_in_range(self) -> None:
        assert 0.0 <= th.QUALITY_REJECT_SCORE <= 10.0
        assert 0.0 <= th.QUALITY_MIN_SCORE <= 10.0

    def test_confidence_thresholds_in_range(self) -> None:
        assert 0.0 < th.MIN_CONFIDENCE <= 1.0
        assert 0.0 < th.ARBITRATION_MIN_CONFIDENCE <= 1.0

    def test_conflict_threshold_in_range(self) -> None:
        assert 0.0 < th.CONFLICT_SIM_THRESHOLD <= 1.0
        assert th.CONFLICT_TOP_K > 0

    def test_prompt_budget_ordering(self) -> None:
        """提示词预算区间必须非空且为正（Q2：1.5-2 万 token）。"""
        assert th.PROMPT_TOKEN_BUDGET_MIN > 0
        assert th.PROMPT_TOKEN_BUDGET_MAX > th.PROMPT_TOKEN_BUDGET_MIN

    def test_layer_weights_cover_twelve_layers(self) -> None:
        """层权必须覆盖 12 层，且全部为正。"""
        assert set(th.LAYER_WEIGHTS) == set(range(1, 13))
        assert all(weight > 0 for weight in th.LAYER_WEIGHTS.values())

    def test_tree_layers_outweigh_open_layers(self) -> None:
        """L1-L4 是强特征，层权必须高于 L5-L12（ADR-0007）。"""
        tree_avg = sum(th.LAYER_WEIGHTS[layer] for layer in (1, 2, 3, 4)) / 4
        open_avg = sum(th.LAYER_WEIGHTS[layer] for layer in range(5, 13)) / 8
        assert tree_avg > open_avg

    def test_drift_thresholds_in_range(self) -> None:
        assert 0.0 < th.DRIFT_TV_THRESHOLD <= 1.0
        assert 0.0 < th.CONTENT_DRIFT_TV_THRESHOLD <= 1.0

    def test_rec_explore_ratio_is_a_fraction(self) -> None:
        assert 0.0 <= th.REC_EXPLORE_RATIO <= 1.0

    def test_weight_sum_tolerance_is_tight_enough(self) -> None:
        """归一化容差必须严于 1e-4，否则「权重和为 1」的口径站不住。"""
        assert 0 < th.WEIGHT_SUM_TOLERANCE < 1e-4

    def test_rec_min_interactions_positive(self) -> None:
        assert th.REC_MIN_INTERACTIONS > 0

    def test_profile_thresholds_positive(self) -> None:
        assert th.PROFILE_COLD_START_THRESHOLD >= 0
        assert th.PROFILE_DECAY_HALFLIFE_DAYS > 0

    def test_max_content_chars_positive(self) -> None:
        assert th.MAX_CONTENT_CHARS > 0


class TestPromptTemplates:
    """提示词模板必须自洽且可解析。"""

    def test_all_templates_are_non_empty(self) -> None:
        for name in ("SYSTEM_TEMPLATE", "USER_TEMPLATE", "ARBITER_TEMPLATE", "SCORING_TEMPLATE"):
            assert getattr(pt, name).strip(), f"{name} 不能为空"

    def test_system_template_demands_json_only(self) -> None:
        """打标提示必须明确要求只输出 JSON，否则解析成功率无法保证。"""
        assert "JSON" in pt.SYSTEM_TEMPLATE

    def test_user_template_has_content_slot(self) -> None:
        assert "{content_block}" in pt.USER_TEMPLATE

    def test_arbiter_template_has_candidate_slot(self) -> None:
        assert "{candidate_block}" in pt.ARBITER_TEMPLATE

    def test_arbiter_template_has_evidence_slot(self) -> None:
        assert "{evidence_block}" in pt.ARBITER_TEMPLATE

    def test_system_template_has_taxonomy_and_schema_slots(self) -> None:
        """system 提示必须能注入标签体系与输出 schema（ADR-0002）。"""
        assert "{taxonomy_block}" in pt.SYSTEM_TEMPLATE
        assert "{schema_block}" in pt.SYSTEM_TEMPLATE

    def test_scoring_template_defines_range(self) -> None:
        assert "0" in pt.SCORING_TEMPLATE and "10" in pt.SCORING_TEMPLATE

    def test_output_schema_is_json_serializable(self) -> None:
        """schema 必须能被 JSON 序列化，否则审计记录写不进去。"""
        assert json.loads(json.dumps(pt.OUTPUT_SCHEMA))

    def test_arbitration_schema_is_json_serializable(self) -> None:
        assert json.loads(json.dumps(pt.ARBITRATION_SCHEMA))

    def test_scoring_schema_is_json_serializable(self) -> None:
        assert json.loads(json.dumps(pt.SCORING_SCHEMA))

    def test_output_schema_declares_required_fields(self) -> None:
        """输出 schema 必须要求 labels 字段，解析器依赖它。"""
        assert "labels" in pt.OUTPUT_SCHEMA["required"]
        assert "labels" in pt.OUTPUT_SCHEMA["properties"]

    def test_output_schema_bounds_label_layer(self) -> None:
        """标签层号必须被约束在 1-12，否则 12 层体系可被越界写入。"""
        item = pt.OUTPUT_SCHEMA["properties"]["labels"]["items"]
        assert item["properties"]["layer"]["minimum"] == 1
        assert item["properties"]["layer"]["maximum"] == 12

    def test_output_schema_bounds_confidence(self) -> None:
        conf = pt.OUTPUT_SCHEMA["properties"]["labels"]["items"]["properties"]["confidence"]
        assert conf["minimum"] == 0
        assert conf["maximum"] == 1

    def test_prompt_versions_are_distinct(self) -> None:
        """四个提示词版本号必须互不相同，否则无法按版本回放。"""
        versions = [
            pt.SYSTEM_PROMPT_VERSION,
            pt.USER_PROMPT_VERSION,
            pt.ARBITER_PROMPT_VERSION,
            pt.SCORING_PROMPT_VERSION,
        ]
        assert len(set(versions)) == len(versions)

    def test_prompt_versions_follow_naming_convention(self) -> None:
        for version in (
            pt.SYSTEM_PROMPT_VERSION,
            pt.USER_PROMPT_VERSION,
            pt.ARBITER_PROMPT_VERSION,
            pt.SCORING_PROMPT_VERSION,
        ):
            assert version.startswith("pt_"), version
            assert version.endswith("_v1"), version

    def test_all_prompt_versions_returns_all_four(self) -> None:
        versions = pt.all_prompt_versions()
        assert len(versions) == 4
        assert set(versions.values()) == {
            pt.SYSTEM_PROMPT_VERSION,
            pt.USER_PROMPT_VERSION,
            pt.ARBITER_PROMPT_VERSION,
            pt.SCORING_PROMPT_VERSION,
        }

    @pytest.mark.parametrize(
        "name",
        ["SYSTEM_TEMPLATE", "USER_TEMPLATE", "ARBITER_TEMPLATE", "SCORING_TEMPLATE"],
    )
    def test_template_formatting_does_not_raise(self, name: str) -> None:
        """模板里的占位符必须能安全格式化（缺参数时给默认占位）。"""
        template = getattr(pt, name)
        defaults = {
            "content_block": "测试内容",
            "candidate_block": "候选标签块",
            "evidence_block": "证据块",
            "schema_block": "schema",
            "taxonomy_block": "标签体系",
            "min_confidence": 0.7,
            "content_type": "article",
            "language": "zh",
            "published_at": "2026-01-01",
            "source": "rss",
        }
        assert template.format(**defaults).strip()
