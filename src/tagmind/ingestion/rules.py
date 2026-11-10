"""规则集装载与热加载（Q15 / P4-PRE-002）。

规则文件是**纯文本**、每行一条：

```
RULE_ID | kind | pattern
```

- ``kind=lit``：字面短语，内部用 ``re.escape`` 编译（大小写不敏感）。
- ``kind=re``：Python 正则，用于谐音/变体/分隔符绕过。

版本号取「有效规则行」的 sha256 前 12 位——注释与空行不参与，因此改注释
不会触发 ``clean_rules_ver`` 变化，改规则内容一定会。这与架构 §6.3 的
``clean_rules_ver = sha256(rules/*.txt)[:8]`` 契约一致（本模块再包一层，把
敏感词与广告词两份文件合成一个 8 位指纹）。

**内置兜底**：文件缺失或不可读时不报错，退回内置最小默认表（P4-PRE-002）。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from tagmind.core.errors import ValidationError
from tagmind.core.logging import get_logger

__all__ = [
    "BUILTIN_AD_RULES",
    "BUILTIN_SENSITIVE_RULES",
    "RuleEntry",
    "RuleMatch",
    "RuleRegistry",
    "RuleSet",
    "parse_rules",
]

logger = get_logger("tagmind.ingestion.rules")

#: 有效规则行的分隔符。
_SEPARATOR: Final[str] = "|"

#: 文件缺失时的内置最小默认表（示例集，保证流水线不中断）。
BUILTIN_SENSITIVE_RULES: Final[str] = """\
SW-B001|lit|代开发票
SW-B002|lit|赌博平台
SW-B003|lit|刷单返利
SW-B004|lit|根治癌症
SW-B005|lit|非法集资
"""

BUILTIN_AD_RULES: Final[str] = """\
AD-B001|lit|点击购买
AD-B002|lit|立即下单
AD-B003|lit|限时秒杀
AD-B004|lit|包邮到家
MK-B001|re|(?:加|联系)[^\\s，。]{0,4}(?:微信|VX|vx)
LN-B001|re|https?://(?:t\\.cn|dwz\\.cn|bit\\.ly)/[A-Za-z0-9]+
"""


@dataclass(frozen=True, slots=True)
class RuleEntry:
    """一条规则。

    Attributes:
        rule_id: 稳定规则 ID（如 ``SW-0001``），用于审计与 QA 复核。
        kind: ``lit`` 或 ``re``。
        pattern: 原始规则文本（字面短语或正则）。
        category: 类别（``sensitive_word`` / ``ad_residue`` / ``marketing``）。
        regex: 编译后的模式（构造时生成，不进比较与 repr）。
    """

    rule_id: str
    kind: str
    pattern: str
    category: str
    regex: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """编译规则；``re`` 类规则编译失败时显式报错，不静默吞掉。"""
        if self.kind not in {"lit", "re"}:
            raise ValidationError(
                "规则 kind 只能是 lit 或 re",
                details={"rule_id": self.rule_id, "kind": self.kind},
            )
        source = re.escape(self.pattern) if self.kind == "lit" else self.pattern
        try:
            compiled = re.compile(source, re.IGNORECASE)
        except re.error as exc:
            raise ValidationError(
                "规则正则无法编译",
                details={"rule_id": self.rule_id, "pattern": self.pattern, "error": str(exc)},
            ) from exc
        object.__setattr__(self, "regex", compiled)

    @property
    def prefix(self) -> str:
        """规则 ID 的类别前缀（``-`` 之前的部分）。"""
        return self.rule_id.split("-", 1)[0].upper()


@dataclass(frozen=True, slots=True)
class RuleMatch:
    """一次规则命中。

    ``span`` 与 ``matched`` 保证可解释：QA 能据此在原文中定位命中词，而不是
    只看到「命中了某条规则」（Q16）。
    """

    rule_id: str
    category: str
    matched: str
    span: tuple[int, int]
    kind: str = "lit"

    def to_dict(self) -> dict[str, object]:
        """转成 JSON 友好的字典。"""
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "matched": self.matched,
            "span": list(self.span),
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class RuleSet:
    """一份规则文件的只读快照。

    ``version`` 由**有效规则行**的内容决定，注释变化不触发版本变化。
    """

    name: str
    entries: tuple[RuleEntry, ...] = ()
    version: str = ""
    source: str = ""

    def __len__(self) -> int:
        """规则条数。"""
        return len(self.entries)

    def by_prefix(self, prefix: str) -> tuple[RuleEntry, ...]:
        """按 ID 前缀筛选规则（如 ``AD`` / ``MK`` / ``LN``）。"""
        wanted = prefix.upper()
        return tuple(entry for entry in self.entries if entry.prefix == wanted)

    def find_all(self, text: str) -> tuple[RuleMatch, ...]:
        """在文本中找出全部命中，按 ``(span.start, rule_id)`` 稳定排序。"""
        hits: list[RuleMatch] = []
        for entry in self.entries:
            for match in entry.regex.finditer(text):
                hits.append(
                    RuleMatch(
                        rule_id=entry.rule_id,
                        category=entry.category,
                        matched=match.group(0),
                        span=(match.start(), match.end()),
                        kind=entry.kind,
                    )
                )
        hits.sort(key=lambda item: (item.span[0], item.rule_id, item.span[1]))
        return tuple(hits)


def _category_for(rule_id: str, fallback: str) -> str:
    """按规则 ID 前缀映射类别，与 ``constants.RULE_CATEGORIES`` 对齐。"""
    prefix = rule_id.split("-", 1)[0].upper()
    mapping = {
        "SW": "sensitive_word",
        "AD": "ad_residue",
        "MK": "marketing",
        "LN": "ad_residue",
        "RP": "repetition",
        "PT": "punctuation",
    }
    return mapping.get(prefix, fallback)


def _effective_lines(text: str) -> list[str]:
    """去掉注释与空行，返回参与版本号计算的有效规则行。"""
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def parse_rules(text: str, *, name: str, fallback_category: str = "sensitive_word") -> RuleSet:
    """解析规则文本为 :class:`RuleSet`。

    Args:
        text: 规则文件全文。
        name: 规则集名称（``sensitive`` / ``ad``）。
        fallback_category: 前缀无法识别时使用的类别。

    Returns:
        解析后的只读快照。格式错误的行会被跳过并记 warning——不因为一行
        写错就让整个服务启动失败。

    Raises:
        ValidationError: 规则文件中没有任何可用规则且没有内置兜底时。
    """
    entries: list[RuleEntry] = []
    for line in _effective_lines(text):
        parts = [part.strip() for part in line.split(_SEPARATOR, 2)]
        if len(parts) != 3 or not all(parts):
            logger.warning("ingestion.rule.malformed", rule_set=name, line=line)
            continue
        rule_id, kind, pattern = parts
        try:
            entries.append(
                RuleEntry(
                    rule_id=rule_id.upper(),
                    kind=kind.lower(),
                    pattern=pattern,
                    category=_category_for(rule_id, fallback_category),
                )
            )
        except ValidationError as exc:
            logger.warning(
                "ingestion.rule.invalid",
                rule_set=name,
                rule_id=rule_id,
                error=exc.message,
            )

    effective = "\n".join(_effective_lines(text))
    version = hashlib.sha256(effective.encode("utf-8")).hexdigest()[:12]
    return RuleSet(name=name, entries=tuple(entries), version=version, source=f"embedded:{name}")


class RuleRegistry:
    """两份规则文件的统一注册表，支持热加载与版本指纹。

    热加载判据用「文件 mtime + size + 内容哈希」，避免只按 mtime 判断时
    遇到文件系统时间戳精度不足（1 秒内的两次修改）而漏更新。
    """

    def __init__(
        self,
        *,
        sensitive_path: Path,
        ad_path: Path,
        hot_reload: bool = True,
    ) -> None:
        """初始化并立即装载两份规则文件。

        Args:
            sensitive_path: 敏感词文件路径。
            ad_path: 广告词文件路径。
            hot_reload: 是否在 ``get()`` 时自动检测文件变化。
        """
        self._sensitive_path = sensitive_path
        self._ad_path = ad_path
        self._hot_reload = hot_reload
        self._sets: dict[str, RuleSet] = {}
        self._signatures: dict[str, str] = {}
        self.reload()

    def _read(self, path: Path, *, builtin: str, name: str, category: str) -> tuple[RuleSet, str]:
        """读取一份规则文件，缺失时退回内置默认表。"""
        try:
            text = path.read_text(encoding="utf-8")
            source = str(path)
        except OSError as exc:
            logger.warning(
                "ingestion.rule.file_unavailable",
                rule_set=name,
                path=str(path),
                error=str(exc),
            )
            text = builtin
            source = "builtin"
        rule_set = parse_rules(text, name=name, fallback_category=category)
        signature = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return (
            RuleSet(
                name=rule_set.name,
                entries=rule_set.entries,
                version=rule_set.version,
                source=source,
            ),
            signature,
        )

    def reload(self) -> bool:
        """强制重新装载两份文件。

        Returns:
            规则版本号是否发生了变化（用于决定是否重算 ``pipeline_version``）。
        """
        previous = self._fingerprint_from_sets()
        sensitive, sig_sensitive = self._read(
            self._sensitive_path,
            builtin=BUILTIN_SENSITIVE_RULES,
            name="sensitive",
            category="sensitive_word",
        )
        ad, sig_ad = self._read(
            self._ad_path,
            builtin=BUILTIN_AD_RULES,
            name="ad",
            category="ad_residue",
        )
        self._sets = {"sensitive": sensitive, "ad": ad}
        self._signatures = {"sensitive": sig_sensitive, "ad": sig_ad}
        changed = self._fingerprint_from_sets() != previous
        if changed:
            logger.info(
                "ingestion.rule.reloaded",
                clean_rules_ver=self.fingerprint(),
                sensitive_count=len(sensitive),
                ad_count=len(ad),
            )
        return changed

    def _refresh_if_changed(self) -> None:
        """检测文件变化并按需重载（热加载）。"""
        if not self._hot_reload:
            return
        changed = False
        for name, path in (("sensitive", self._sensitive_path), ("ad", self._ad_path)):
            try:
                signature = hashlib.sha256(
                    path.read_text(encoding="utf-8").encode("utf-8")
                ).hexdigest()
            except OSError:
                continue
            if signature != self._signatures.get(name):
                changed = True
                break
        if changed:
            self.reload()

    def get(self, name: str) -> RuleSet:
        """取一份规则集快照。

        Args:
            name: ``sensitive`` 或 ``ad``。

        Returns:
            规则集快照。

        Raises:
            ValidationError: 名称不存在。
        """
        self._refresh_if_changed()
        try:
            return self._sets[name]
        except KeyError as exc:
            raise ValidationError(
                "未知规则集", details={"name": name, "available": sorted(self._sets)}
            ) from exc

    @property
    def sensitive(self) -> RuleSet:
        """敏感词规则集。"""
        return self.get("sensitive")

    @property
    def ad(self) -> RuleSet:
        """广告词规则集。"""
        return self.get("ad")

    def fingerprint(self) -> str:
        """返回两份规则合成后的 8 位指纹，即 ``clean_rules_ver``。

        取指纹前先做一次热加载检测——否则调用方拿到的会是**旧**指纹，
        ``pipeline_version`` 会静默漏掉规则变更（架构 §6.3）。
        """
        self._refresh_if_changed()
        return self._fingerprint_from_sets()

    def _fingerprint_from_sets(self) -> str:
        """从当前内存快照算指纹，**不触发**热加载。

        与 :meth:`fingerprint` 分离是必要的：``reload()`` 内部要算「修改前」
        的指纹，若在此处再调 ``_refresh_if_changed()`` 会形成递归重载。
        """
        if not self._sets:
            return hashlib.sha256(b"").hexdigest()[:8]
        joined = ":".join(f"{name}={self._sets[name].version}" for name in sorted(self._sets))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:8]

    @property
    def clean_rules_version(self) -> str:
        """``PipelineVersion.clean_rules_ver`` 的取值。"""
        return self.fingerprint()

    def iter_matches(
        self, text: str, *, categories: tuple[str, ...] | None = None
    ) -> Iterator[RuleMatch]:
        """在文本中按顺序迭代全部规则命中。

        Args:
            text: 待检文本。
            categories: 只保留这些类别；``None`` 表示全部。

        Yields:
            按位置排序的 :class:`RuleMatch`。
        """
        hits: list[RuleMatch] = []
        for name in ("sensitive", "ad"):
            for match in self.get(name).find_all(text):
                if categories is None or match.category in categories:
                    hits.append(match)
        hits.sort(key=lambda item: (item.span[0], item.rule_id))
        yield from hits
