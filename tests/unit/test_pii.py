"""``ingestion/pii.py`` 单元测试（P4-SEC-001）。

覆盖两个脱敏卡点、占位符替换、``pii_fields`` 只记类型不记原值、
``pii_types`` 过滤、无 PII 文本不变、以及映射表只存哈希。
全部纯函数，不需要 PostgreSQL。
"""

from __future__ import annotations

import re

import pytest

from tagmind.core.errors import ValidationError
from tagmind.core.security import PII_PATTERNS, mask_pii
from tagmind.ingestion.pii import (
    PIIMaskRecord,
    PIIMaskResult,
    assert_no_raw_pii,
    build_mask_map,
    has_raw_pii,
    mask_for_llm,
    mask_for_storage,
)


class TestMaskForStorage:
    """落库前脱敏（卡点 1）。"""

    def test_phone_is_replaced_by_placeholder(self) -> None:
        result = mask_for_storage("联系电话 13800138000，欢迎咨询")
        assert "[PHONE_1]" in result.text
        assert "13800138000" not in result.text
        assert result.masked is True

    def test_email_is_replaced(self) -> None:
        result = mask_for_storage("邮箱 test.user@example.com 已收到")
        assert "[EMAIL_1]" in result.text
        assert "test.user@example.com" not in result.text

    def test_id_card_is_replaced(self) -> None:
        result = mask_for_storage("身份证 110101199003075678 已核验")
        assert "[ID_CARD_1]" in result.text
        assert "110101199003075678" not in result.text

    def test_bank_card_is_replaced(self) -> None:
        result = mask_for_storage("卡号 6222020200001234567")
        assert "[BANK_CARD_1]" in result.text
        assert "6222020200001234567" not in result.text

    def test_multiple_types_are_all_replaced(self) -> None:
        text = "手机 13800138000 邮箱 a@b.com 身份证 110101199003075678"
        result = mask_for_storage(text)
        for original in ("13800138000", "a@b.com", "110101199003075678"):
            assert original not in result.text, f"{original} 未被脱敏"
        assert result.count == 3
        assert len(result.placeholders) == 3

    @pytest.mark.parametrize("pii_type", ["phone", "id_card", "email", "bank_card"])
    def test_fields_record_type_only(self, pii_type: str) -> None:
        samples = {
            "phone": "手机 13900139000",
            "id_card": "证件 110101199003075678",
            "email": "信箱 x@y.org",
            "bank_card": "卡号 6222020200001234567",
        }
        result = mask_for_storage(samples[pii_type])
        assert pii_type in result.fields
        # 原值绝不能出现在 pii_fields / placeholders / 返回对象里
        for value in re.findall(r"\d{11,}|[\w.]+@[\w.]+", samples[pii_type]):
            assert value not in str(result.fields)
            assert value not in str(result.placeholders)
        assert result.pii_fields == result.fields

    def test_plain_text_is_unchanged(self) -> None:
        text = "这是一段不含任何个人信息的普通正文。"
        result = mask_for_storage(text)
        assert result.text == text
        assert result.masked is False
        assert result.fields == ()
        assert result.count == 0
        assert result.placeholders == ()

    def test_placeholders_are_not_remasked(self) -> None:
        """已脱敏文本再跑一次不应二次替换（占位符不含 PII 模式）。"""
        once = mask_for_storage("电话 13800138000").text
        twice = mask_for_storage(once).text
        assert once == twice

    def test_sequential_placeholders_are_numbered(self) -> None:
        result = mask_for_storage("甲 13800138000 乙 13900139000")
        assert result.placeholders == ("[PHONE_1]", "[PHONE_2]")

    def test_result_is_frozen(self) -> None:
        result = mask_for_storage("无")
        with pytest.raises(AttributeError):
            result.text = "改不了"  # type: ignore[misc]


class TestMaskForLlm:
    """送 LLM 前脱敏（卡点 2）。"""

    def test_llm_checkpoint_masks_residual_pii(self) -> None:
        result = mask_for_llm("请发到 user@corp.com")
        assert "[EMAIL_1]" in result.text
        assert "user@corp.com" not in result.text

    def test_both_checkpoints_produce_same_text(self) -> None:
        raw = "联系 13800138000 或 a@b.com"
        assert mask_for_storage(raw).text == mask_for_llm(raw).text

    def test_llm_checkpoint_on_clean_text_is_noop(self) -> None:
        cleaned = mask_for_storage("电话 13800138000").text
        result = mask_for_llm(cleaned)
        assert result.text == cleaned, "已脱敏文本不应被二次改写"
        assert has_raw_pii(result.text) is False
        # ``count`` 统计的是文本中的占位符总数（含调用前已存在的），
        # 因此这里是 1；语义上「没有新增脱敏」由 text 不变与 fields 为空共同表达。
        assert result.fields == ()


class TestHasRawPii:
    """``has_raw_pii`` 的判定语义。"""

    def test_true_on_raw_phone(self) -> None:
        assert has_raw_pii("电话 13800138000") is True

    def test_false_on_placeholder(self) -> None:
        assert has_raw_pii("电话 [PHONE_1]") is False

    def test_false_on_plain_text(self) -> None:
        assert has_raw_pii("普通正文，无敏感信息") is False

    def test_true_when_any_pattern_matches(self) -> None:
        """每个 PII 模式都能在对应样本上命中；``has_raw_pii`` 用 any 短路。"""
        samples = {
            "id_card": "证件 110101199003075678",
            "bank_card": "卡号 6222020200001234567",
            "email": "邮箱 a@b.com",
            "phone": "手机 13800138000",
            "address": "地址 广东省深圳市南山区科技园南区1号",
        }
        assert set(samples) == set(PII_PATTERNS)
        for pii_type, text in samples.items():
            assert PII_PATTERNS[pii_type].search(text) is not None, pii_type
            assert has_raw_pii(text) is True, pii_type

    def test_assert_no_raw_pii_raises_on_leak(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            assert_no_raw_pii("发给 13800138000")
        assert "PII" in excinfo.value.message
        assert excinfo.value.details["checkpoint"] == "llm"

    def test_assert_no_raw_pii_passes_masked_text(self) -> None:
        cleaned = mask_for_storage("发给 13800138000").text
        assert_no_raw_pii(cleaned)

    def test_assert_context_is_recorded(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            assert_no_raw_pii("邮箱 a@b.com", context="user_prompt")
        assert excinfo.value.details["context"] == "user_prompt"


class TestMaskMap:
    """脱敏映射表：只存哈希，不存原值。"""

    def test_record_contains_hash_not_original(self) -> None:
        records = build_mask_map("手机 13800138000")
        assert len(records) == 1
        record = records[0]
        assert isinstance(record, PIIMaskRecord)
        assert record.pii_type == "phone"
        assert record.placeholder == "[PHONE_1]"
        assert "13800138000" not in str(record)
        assert len(record.original_hash) == 64

    def test_placeholder_matches_masked_text(self) -> None:
        raw = "甲 13800138000 乙 13900139000 丙 a@b.com"
        records = build_mask_map(raw)
        masked = mask_for_storage(raw).text
        # ``build_mask_map`` 按 PII_PATTERNS 的类型顺序收集（email 先于 phone），
        # 因此占位符顺序与文本出现顺序不同；用集合比对二者是否一致。
        assert {item.placeholder for item in records} == {
            "[PHONE_1]",
            "[PHONE_2]",
            "[EMAIL_1]",
        }
        for item in records:
            assert item.placeholder in masked

    def test_same_value_produces_same_hash(self) -> None:
        first = build_mask_map("电话 13800138000")
        second = build_mask_map("电话 13800138000")
        assert first == second

    def test_no_pii_produces_no_records(self) -> None:
        assert build_mask_map("普通正文") == ()

    def test_record_to_dict_shape(self) -> None:
        record = build_mask_map("邮箱 a@b.com")[0].to_dict()
        assert set(record) == {"placeholder", "pii_type", "original_hash"}


class TestCoreSecurityCompatibility:
    """与阶段 0 ``core.security.mask_pii`` 的契约一致性。"""

    def test_storage_mask_matches_core_helper(self) -> None:
        raw = "手机 13800138000 邮箱 a@b.com"
        expected, _ = mask_pii(raw)
        assert mask_for_storage(raw).text == expected

    def test_pii_types_filter_is_honoured(self) -> None:
        """``mask_pii(pii_types=...)`` 只处理指定类型；本模块不绕过它。"""
        masked, hits = mask_pii("手机 13800138000 邮箱 a@b.com", pii_types=("phone",))
        assert "[PHONE_1]" in masked
        assert "a@b.com" in masked, "未选中的 email 类型不应被替换"
        assert hits == ["phone"]

    def test_mask_result_is_a_domain_value_object(self) -> None:
        result = PIIMaskResult(text="x", fields=("phone",))
        assert result.pii_fields == ("phone",)
        assert result.masked is False
