"""``ingestion/rules.py`` 单元测试（Q15 / Q16 / P4-PRE-002）。

覆盖：规则解析与加载、内置兜底、热加载后版本指纹变化、注释不影响版本、
四判据广告词命中返回 ``rule_id`` + 位置 + 命中词、规则类别映射与排序。
全部使用 ``tmp_path`` 里的临时文件，不依赖仓库里的真实规则集。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tagmind.core.errors import ValidationError
from tagmind.ingestion.rules import (
    BUILTIN_AD_RULES,
    BUILTIN_SENSITIVE_RULES,
    RuleEntry,
    RuleMatch,
    RuleRegistry,
    RuleSet,
    parse_rules,
)

#: 覆盖 Q16 四类判据的最小规则集：
#: AD = 高频广告短语（判据①）、MK = 营销话术正则（判据②）、
#: LN = 短链/联系方式（判据③）、SW = 敏感词（一票否决）。
FOUR_CRITERIA_RULES = """\
# 四判据示例
AD-0001|lit|点击购买
AD-0002|lit|限时秒杀
MK-0001|re|立即[抢下][购单]
LN-0001|re|https?://t\\.cn/[A-Za-z0-9]+
SW-0001|lit|代开发票
"""


def _write_rules(tmp_path: Path, *, sensitive: str, ad: str) -> tuple[Path, Path]:
    """在临时目录写两份规则文件。"""
    sensitive_path = tmp_path / "sensitive_words.txt"
    ad_path = tmp_path / "ad_words.txt"
    sensitive_path.write_text(sensitive, encoding="utf-8")
    ad_path.write_text(ad, encoding="utf-8")
    return sensitive_path, ad_path


def _registry(tmp_path: Path, *, hot_reload: bool = True) -> RuleRegistry:
    sensitive, ad = _write_rules(
        tmp_path, sensitive="SW-0001|lit|代开发票\n", ad=FOUR_CRITERIA_RULES
    )
    return RuleRegistry(sensitive_path=sensitive, ad_path=ad, hot_reload=hot_reload)


class TestParseRules:
    """纯文本解析。"""

    def test_literal_and_regex_entries(self) -> None:
        rule_set = parse_rules(FOUR_CRITERIA_RULES, name="ad")
        assert len(rule_set) == 5
        kinds = {entry.rule_id: entry.kind for entry in rule_set.entries}
        assert kinds["AD-0001"] == "lit"
        assert kinds["MK-0001"] == "re"

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        text = "# 注释一\n\n  # 注释二\nAD-0001|lit|点击购买\n"
        assert len(parse_rules(text, name="ad")) == 1

    def test_version_is_content_hash_of_effective_lines(self) -> None:
        text = "AD-0001|lit|点击购买\n"
        expected = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]
        assert parse_rules(text, name="ad").version == expected

    def test_comment_change_does_not_change_version(self) -> None:
        first = parse_rules("# v1\nAD-0001|lit|点击购买\n", name="ad")
        second = parse_rules("# v2 改了注释\nAD-0001|lit|点击购买\n", name="ad")
        assert first.version == second.version

    def test_rule_content_change_changes_version(self) -> None:
        first = parse_rules("AD-0001|lit|点击购买\n", name="ad")
        second = parse_rules("AD-0001|lit|立即下单\n", name="ad")
        assert first.version != second.version

    def test_malformed_line_is_skipped_without_raising(self) -> None:
        rule_set = parse_rules("这行格式不对\nAD-0001|lit|点击购买\n", name="ad")
        assert len(rule_set) == 1

    def test_invalid_regex_is_skipped_not_fatal(self) -> None:
        """坏正则只跳过该行并记 warning，不让整份规则集加载失败。"""
        rule_set = parse_rules("MK-0001|re|([未闭合\nAD-0001|lit|点击购买\n", name="ad")
        assert [entry.rule_id for entry in rule_set.entries] == ["AD-0001"]

    def test_unknown_kind_is_skipped(self) -> None:
        rule_set = parse_rules("AD-0001|xxx|点击购买\nAD-0002|lit|立即下单\n", name="ad")
        assert [entry.rule_id for entry in rule_set.entries] == ["AD-0002"]

    def test_rule_entry_rejects_bad_kind(self) -> None:
        """直接构造 RuleEntry 时非法 kind 必须显式报错。"""
        with pytest.raises(ValidationError):
            RuleEntry("AD-0001", "xxx", "点击购买", "ad_residue")

    def test_rule_entry_rejects_broken_regex(self) -> None:
        with pytest.raises(ValidationError):
            RuleEntry("MK-0001", "re", "([未闭合", "marketing")

    def test_category_is_derived_from_prefix(self) -> None:
        rule_set = parse_rules(FOUR_CRITERIA_RULES, name="ad")
        categories = {entry.rule_id: entry.category for entry in rule_set.entries}
        assert categories["AD-0001"] == "ad_residue"
        assert categories["MK-0001"] == "marketing"
        assert categories["LN-0001"] == "ad_residue"
        assert categories["SW-0001"] == "sensitive_word"

    def test_by_prefix_filters(self) -> None:
        rule_set = parse_rules(FOUR_CRITERIA_RULES, name="ad")
        assert len(rule_set.by_prefix("AD")) == 2
        assert len(rule_set.by_prefix("MK")) == 1
        assert rule_set.by_prefix("ZZ") == ()

    def test_entry_prefix_property(self) -> None:
        entry = RuleEntry("sw-0001", "lit", "代开发票", "sensitive_word")
        assert entry.prefix == "SW"

    def test_rule_set_len(self) -> None:
        assert len(RuleSet(name="empty")) == 0


class TestExplainableMatches:
    """Q16：命中项必须给出 rule_id + 位置 + 命中词。"""

    def test_high_frequency_ad_phrase_is_explainable(self) -> None:
        rule_set = parse_rules(FOUR_CRITERIA_RULES, name="ad")
        hits = rule_set.find_all("新款上市，点击购买 >>>")
        ad_hits = [hit for hit in hits if hit.rule_id == "AD-0001"]
        assert len(ad_hits) == 1
        hit = ad_hits[0]
        assert hit.matched == "点击购买"
        assert hit.span == (5, 9)
        assert hit.category == "ad_residue"

    def test_marketing_regex_is_explainable(self) -> None:
        rule_set = parse_rules(FOUR_CRITERIA_RULES, name="ad")
        hits = rule_set.find_all("现在立即抢购，别犹豫")
        match = next(hit for hit in hits if hit.rule_id == "MK-0001")
        assert match.matched == "立即抢购"
        assert match.span[0] == 2
        assert match.kind == "re"

    def test_short_link_is_explainable(self) -> None:
        rule_set = parse_rules(FOUR_CRITERIA_RULES, name="ad")
        hits = rule_set.find_all("详情见 https://t.cn/A6xyz")
        match = next(hit for hit in hits if hit.rule_id == "LN-0001")
        assert match.matched == "https://t.cn/A6xyz"
        assert match.span[1] - match.span[0] == len("https://t.cn/A6xyz")

    def test_four_criteria_all_hit_on_one_text(self) -> None:
        rule_set = parse_rules(FOUR_CRITERIA_RULES, name="ad")
        text = "点击购买 立即抢购 https://t.cn/A6xyz 代开发票"
        hits = rule_set.find_all(text)
        prefixes = {hit.rule_id.split("-")[0] for hit in hits}
        assert prefixes == {"AD", "MK", "LN", "SW"}

    def test_span_can_slice_original_text(self) -> None:
        """位置必须能直接切回原文——这是 QA 复核的前提。"""
        rule_set = parse_rules(FOUR_CRITERIA_RULES, name="ad")
        text = "开头 限时秒杀 结尾"
        hit = next(h for h in rule_set.find_all(text) if h.rule_id == "AD-0002")
        assert text[hit.span[0] : hit.span[1]] == hit.matched

    def test_hits_are_sorted_by_position(self) -> None:
        rule_set = parse_rules(FOUR_CRITERIA_RULES, name="ad")
        hits = rule_set.find_all("限时秒杀 然后 点击购买")
        assert [hit.span[0] for hit in hits] == sorted(hit.span[0] for hit in hits)

    def test_no_hit_on_clean_text(self) -> None:
        rule_set = parse_rules(FOUR_CRITERIA_RULES, name="ad")
        assert rule_set.find_all("这是一段正常的科技资讯正文。") == ()

    def test_rule_match_to_dict_is_json_friendly(self) -> None:
        hit = RuleMatch("AD-0001", "ad_residue", "点击购买", (5, 9))
        payload = hit.to_dict()
        assert payload["rule_id"] == "AD-0001"
        assert payload["span"] == [5, 9]
        assert payload["matched"] == "点击购买"

    def test_literal_pattern_is_escaped(self) -> None:
        """字面规则里的正则元字符不能被当成正则执行。"""
        rule_set = parse_rules("AD-0001|lit|a.b\n", name="ad")
        assert rule_set.find_all("a.b") != ()
        assert rule_set.find_all("axb") == ()


class TestRuleRegistry:
    """装载、兜底与热加载。"""

    def test_loads_both_rule_sets(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        assert len(registry.sensitive) == 1
        assert len(registry.ad) == 5

    def test_fingerprint_is_eight_hex_chars(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        version = registry.fingerprint()
        assert len(version) == 8
        assert all(char in "0123456789abcdef" for char in version)
        assert registry.clean_rules_version == version

    def test_missing_files_fall_back_to_builtin(self, tmp_path: Path) -> None:
        registry = RuleRegistry(
            sensitive_path=tmp_path / "missing_sw.txt",
            ad_path=tmp_path / "missing_ad.txt",
        )
        assert len(registry.sensitive) > 0
        assert len(registry.ad) > 0
        assert registry.sensitive.source == "builtin"
        assert registry.ad.source == "builtin"

    def test_builtin_defaults_are_parseable(self) -> None:
        assert len(parse_rules(BUILTIN_SENSITIVE_RULES, name="sensitive")) >= 3
        assert len(parse_rules(BUILTIN_AD_RULES, name="ad")) >= 3

    def test_hot_reload_changes_version(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path, hot_reload=True)
        before = registry.fingerprint()
        ad_path = tmp_path / "ad_words.txt"
        ad_path.write_text(
            ad_path.read_text(encoding="utf-8") + "AD-9999|lit|新增广告词\n", encoding="utf-8"
        )
        after = registry.fingerprint()
        assert after != before, "规则文件变化后 clean_rules_ver 必须变化"
        assert len(registry.ad) == 6

    def test_hot_reload_disabled_keeps_snapshot(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path, hot_reload=False)
        before = registry.fingerprint()
        ad_path = tmp_path / "ad_words.txt"
        ad_path.write_text(
            ad_path.read_text(encoding="utf-8") + "AD-9999|lit|新增广告词\n", encoding="utf-8"
        )
        assert registry.fingerprint() == before
        assert len(registry.ad) == 5

    def test_explicit_reload_returns_change_flag(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path, hot_reload=False)
        assert registry.reload() is False, "内容未变时不应报告变化"
        ad_path = tmp_path / "ad_words.txt"
        ad_path.write_text(
            ad_path.read_text(encoding="utf-8") + "AD-9999|lit|另一个新词\n", encoding="utf-8"
        )
        assert registry.reload() is True

    def test_comment_only_edit_does_not_change_fingerprint(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path, hot_reload=True)
        before = registry.fingerprint()
        ad_path = tmp_path / "ad_words.txt"
        ad_path.write_text(ad_path.read_text(encoding="utf-8") + "# 仅新增注释\n", encoding="utf-8")
        assert registry.fingerprint() == before

    def test_unknown_rule_set_raises(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        with pytest.raises(ValidationError):
            registry.get("does_not_exist")

    def test_iter_matches_filters_by_category(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        hits = list(registry.iter_matches("点击购买 代开发票", categories=("ad_residue",)))
        assert len(hits) == 1
        assert hits[0].rule_id == "AD-0001"

    def test_iter_matches_is_sorted_by_position(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        hits = list(registry.iter_matches("代开发票 点击购买"))
        assert [hit.span[0] for hit in hits] == sorted(hit.span[0] for hit in hits)
