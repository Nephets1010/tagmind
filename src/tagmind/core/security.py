"""安全原语：API Key 校验、PII 正则族、路径白名单、令牌桶限流。

这一层不依赖任何 Web 框架，因此可以在单元测试里直接调用，也可以在
Celery worker 里复用同一套判定逻辑。

正则族与 §5.7 ``pii_mask_map.pii_type`` 的取值域严格对应：
``phone`` / ``id_card`` / ``email`` / ``bank_card`` / ``name`` / ``address``。
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Final

from tagmind.core.errors import AuthError, PermissionDenied

__all__ = [
    "PII_PATTERNS",
    "PathGuard",
    "RateLimiter",
    "TokenBucket",
    "mask_pii",
    "verify_api_key",
]


#: PII 正则族：类型 → 匹配模式。顺序即替换优先级。
PII_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "id_card": re.compile(r"(?<!\w)\d{17}[\dXx](?!\w)"),
    "bank_card": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    "email": re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    "phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "address": re.compile(
        r"[一-鿿]{2,}(?:省|自治区)?[一-鿿]{2,}"
        r"(?:市|自治州)[一-鿿]{2,}(?:区|县|市)[^\s，。！？,.\n]{2,}"
    ),
}

#: 人名不做正则匹配（中文人名歧义太大），只做占位符登记。
PII_TYPES: Final[tuple[str, ...]] = (
    "phone",
    "id_card",
    "email",
    "bank_card",
    "name",
    "address",
)


def mask_pii(text: str, *, pii_types: tuple[str, ...] | None = None) -> tuple[str, list[str]]:
    """把文本中的 PII 替换为占位符。

    Args:
        text: 原始文本。
        pii_types: 只处理这些类型；``None`` 表示全部类型。

    Returns:
        ``(脱敏后文本, 命中类型列表)``；命中类型按出现顺序去重。
    """
    selected = pii_types if pii_types is not None else tuple(PII_PATTERNS)
    counters: dict[str, int] = {}
    hits: list[str] = []
    result = text
    for pii_type in selected:
        pattern = PII_PATTERNS.get(pii_type)
        if pattern is None:
            continue

        def _replace(match: re.Match[str], pii_type: str = pii_type) -> str:
            counters[pii_type] = counters.get(pii_type, 0) + 1
            hits.append(pii_type)
            return f"[{pii_type.upper()}_{counters[pii_type]}]"

        result = pattern.sub(_replace, result)
    return result, sorted(set(hits))


def verify_api_key(provided: str | None, expected: str | None) -> None:
    """校验 API Key。

    Args:
        provided: 客户端提供的密钥。
        expected: 服务端配置的密钥；为空表示未启用鉴权。

    Raises:
        AuthError: 未提供密钥或密钥不匹配。
    """
    if not expected:
        return
    if not provided:
        raise AuthError("缺少 API Key", details={"reason": "missing_api_key"})
    # 常数时间比较，避免通过响应时间侧信道逐字节猜测密钥。
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise AuthError("API Key 无效", details={"reason": "invalid_api_key"})


def hash_user_id(user_id: str) -> str:
    """把 user_id 哈希成日志可用的短标识（不泄漏原始 ID）。"""
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class PathGuard:
    """路径白名单守卫。

    用于阻断 ``../`` 穿越与绝对路径读取。判定基于「规范化后的路径必须落在
    允许的根目录之下」，而不是简单的字符串前缀匹配（后者会被
    ``/data/root_evil`` 这类兄弟目录绕过）。
    """

    allowed_roots: tuple[str, ...]
    _resolved: tuple[str, ...] = field(default=(), init=False, repr=False)

    def __post_init__(self) -> None:
        from pathlib import Path

        self._resolved = tuple(str(Path(root).resolve()) for root in self.allowed_roots)

    def is_allowed(self, candidate: str) -> bool:
        """判断候选路径是否落在允许根目录之下。"""
        from pathlib import Path

        try:
            resolved = Path(candidate).resolve()
        except OSError:  # pragma: no cover - 路径解析极少失败
            return False
        return any(
            resolved == Path(root) or Path(root) in resolved.parents for root in self._resolved
        )

    def require(self, candidate: str) -> None:
        """不在白名单内时抛 :class:`PermissionDenied`。"""
        if not self.is_allowed(candidate):
            raise PermissionDenied("路径不在允许范围内", details={"reason": "path_not_allowed"})


@dataclass(slots=True)
class TokenBucket:
    """令牌桶。

    以「速率 + 容量」限制单位时间内的调用次数，允许突发但不允许长期超发。
    """

    rate: float
    capacity: float
    _tokens: float = field(default=0.0, init=False)
    _updated_at: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError("rate 必须为正数")
        if self.capacity <= 0:
            raise ValueError("capacity 必须为正数")
        self._tokens = self.capacity
        self._updated_at = time.monotonic()

    def consume(self, amount: float = 1.0) -> bool:
        """尝试取令牌。

        Args:
            amount: 本次需要的令牌数。

        Returns:
            成功返回 ``True``，桶空返回 ``False``。

        Raises:
            ValueError: ``amount`` 非正或非有限数。负值此前会反向补充令牌
                （``consume(-100)`` 直接把桶填满），等于绕过限流。
        """
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("amount 必须为正的有限数")
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._updated_at
            self._updated_at = now
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            if self._tokens >= amount:
                self._tokens -= amount
                return True
            return False

    @property
    def available(self) -> float:
        """当前可用令牌数（近似值）。"""
        with self._lock:
            return self._tokens


class RateLimiter:
    """按 key（IP / 用户 ID）分桶的限流器。

    每个 key 一个独立令牌桶；桶数量无上限但会惰性清理，避免长期运行内存增长。
    """

    def __init__(self, *, rate: float, capacity: float, ttl_s: float = 3600.0) -> None:
        """初始化限流器。

        Args:
            rate: 每个 key 每秒允许的请求数。
            capacity: 单个 key 的桶容量。
            ttl_s: 空闲桶的存活时间（秒）。
        """
        self._rate = rate
        self._capacity = capacity
        self._ttl_s = ttl_s
        self._buckets: dict[str, tuple[TokenBucket, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """判断某个 key 本次请求是否放行。"""
        now = time.monotonic()
        with self._lock:
            entry = self._buckets.get(key)
            if entry is None:
                bucket = TokenBucket(rate=self._rate, capacity=self._capacity)
                self._buckets[key] = (bucket, now)
                return bucket.consume()
            bucket, last_seen = entry
            if now - last_seen > self._ttl_s:
                bucket = TokenBucket(rate=self._rate, capacity=self._capacity)
                self._buckets[key] = (bucket, now)
                return bucket.consume()
            self._buckets[key] = (bucket, now)
            return bucket.consume()

    def cleanup(self) -> int:
        """清理空闲桶，返回清理数量。"""
        now = time.monotonic()
        with self._lock:
            stale = [key for key, (_, seen) in self._buckets.items() if now - seen > self._ttl_s]
            for key in stale:
                del self._buckets[key]
            return len(stale)
