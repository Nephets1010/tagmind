"""安全与访问控制模块的单元测试。

覆盖 ``core/security.py`` 的全部公开能力：PII 脱敏、API Key 校验、
用户 ID 哈希、路径白名单、令牌桶与限流器。

这些能力的共同点是「拒绝必须是显式且可断言的」——静默放行比误拒更危险，
因此每个拒绝路径都需要独立断言。
"""

from __future__ import annotations

import time

import pytest

from tagmind.core.errors import AuthError, PermissionDenied
from tagmind.core.security import (
    PII_PATTERNS,
    PathGuard,
    RateLimiter,
    TokenBucket,
    hash_user_id,
    mask_pii,
    verify_api_key,
)


class TestMaskPii:
    """PII 脱敏必须替换原值、且不泄漏命中类型以外的细节。"""

    def test_masks_phone_number(self) -> None:
        text = "联系电话 13800138000 请勿外传"
        masked, hits = mask_pii(text)
        assert "13800138000" not in masked
        assert "phone" in hits

    def test_masks_email(self) -> None:
        masked, hits = mask_pii("邮箱 zhangsan@example.com")
        assert "zhangsan@example.com" not in masked
        assert "email" in hits

    def test_masks_id_card(self) -> None:
        masked, hits = mask_pii("身份证 110101199003071234")
        assert "110101199003071234" not in masked
        assert "id_card" in hits

    def test_placeholder_is_numbered_per_type(self) -> None:
        masked, _ = mask_pii("13800138000 和 13900139000")
        assert "[PHONE_1]" in masked
        assert "[PHONE_2]" in masked

    def test_hit_types_are_deduplicated_and_sorted(self) -> None:
        _, hits = mask_pii("电话 13800138000 邮箱 a@b.com 电话 13900139000")
        assert hits == sorted(set(hits))
        assert len(hits) == len(set(hits))

    def test_text_without_pii_is_unchanged(self) -> None:
        original = "这是一条完全正常的科技新闻，没有任何敏感信息。"
        masked, hits = mask_pii(original)
        assert masked == original
        assert hits == []

    def test_type_filter_restricts_detection(self) -> None:
        text = "电话 13800138000 邮箱 a@b.com"
        masked, hits = mask_pii(text, pii_types=("email",))
        assert "a@b.com" not in masked
        assert "13800138000" in masked
        assert hits == ["email"]

    def test_unknown_type_is_ignored(self) -> None:
        masked, hits = mask_pii("13800138000", pii_types=("not_a_real_type",))
        assert masked == "13800138000"
        assert hits == []

    def test_every_declared_pattern_is_usable(self) -> None:
        """每个登记的正则都必须能编译，避免配置里藏一个坏表达式。"""
        import re

        for name, pattern in PII_PATTERNS.items():
            assert isinstance(name, str)
            re.compile(pattern)


class TestVerifyApiKey:
    """API Key 校验的三条路径：未配置 / 正确 / 错误。"""

    def test_no_expected_key_allows_anonymous(self) -> None:
        """服务端未配置密钥时放行（演示模式）。"""
        assert verify_api_key(None, None) is None
        assert verify_api_key("anything", "") is None

    def test_matching_key_passes(self) -> None:
        assert verify_api_key("secret-key", "secret-key") is None

    def test_missing_key_raises(self) -> None:
        with pytest.raises(AuthError) as excinfo:
            verify_api_key(None, "secret-key")
        assert excinfo.value.details.get("reason") == "missing_api_key"

    def test_wrong_key_raises(self) -> None:
        with pytest.raises(AuthError) as excinfo:
            verify_api_key("wrong", "secret-key")
        assert excinfo.value.details.get("reason") == "invalid_api_key"

    def test_prefix_of_correct_key_is_rejected(self) -> None:
        """短前缀不能通过，避免把比较实现成 startswith。"""
        with pytest.raises(AuthError):
            verify_api_key("secret", "secret-key")

    def test_longer_key_is_rejected(self) -> None:
        with pytest.raises(AuthError):
            verify_api_key("secret-key-extra", "secret-key")


class TestHashUserId:
    """用户 ID 哈希必须稳定、不可逆、不可区分原值。"""

    def test_hash_is_deterministic(self) -> None:
        assert hash_user_id("u-123") == hash_user_id("u-123")

    def test_hash_differs_for_different_ids(self) -> None:
        assert hash_user_id("u-123") != hash_user_id("u-456")

    def test_hash_does_not_leak_original(self) -> None:
        assert "u-123" not in hash_user_id("u-123")

    def test_hash_length_is_bounded(self) -> None:
        assert len(hash_user_id("u-123")) == 16


class TestPathGuard:
    """路径白名单必须拦住穿越与兄弟目录绕过。"""

    def test_allows_path_inside_root(self, tmp_path) -> None:
        guard = PathGuard(allowed_roots=(str(tmp_path),))
        assert guard.is_allowed(str(tmp_path / "data.txt")) is True

    def test_allows_root_itself(self, tmp_path) -> None:
        guard = PathGuard(allowed_roots=(str(tmp_path),))
        assert guard.is_allowed(str(tmp_path)) is True

    def test_rejects_traversal(self, tmp_path) -> None:
        guard = PathGuard(allowed_roots=(str(tmp_path),))
        assert guard.is_allowed(str(tmp_path / ".." / "secret.txt")) is False

    def test_rejects_sibling_directory_with_prefix(self, tmp_path) -> None:
        """`/data/root_evil` 这类兄弟目录必须被拒——字符串前缀匹配会漏掉它。"""
        root = tmp_path / "root"
        sibling = tmp_path / "root_evil"
        root.mkdir()
        sibling.mkdir()
        guard = PathGuard(allowed_roots=(str(root),))
        assert guard.is_allowed(str(sibling / "secret.txt")) is False

    def test_rejects_absolute_outside_path(self, tmp_path) -> None:
        guard = PathGuard(allowed_roots=(str(tmp_path),))
        assert guard.is_allowed("/etc/passwd") is False

    def test_require_raises_permission_denied(self, tmp_path) -> None:
        guard = PathGuard(allowed_roots=(str(tmp_path),))
        with pytest.raises(PermissionDenied) as excinfo:
            guard.require("/etc/passwd")
        assert excinfo.value.details.get("reason") == "path_not_allowed"

    def test_require_passes_for_allowed_path(self, tmp_path) -> None:
        guard = PathGuard(allowed_roots=(str(tmp_path),))
        assert guard.require(str(tmp_path / "ok.txt")) is None


class TestTokenBucket:
    """令牌桶必须精确控制突发量与补充速率。"""

    def test_starts_full(self) -> None:
        bucket = TokenBucket(capacity=3, rate=1.0)
        assert bucket.available == pytest.approx(3.0)

    def test_consume_up_to_capacity(self) -> None:
        bucket = TokenBucket(capacity=3, rate=1.0)
        assert bucket.consume() is True
        assert bucket.consume() is True
        assert bucket.consume() is True

    def test_consume_beyond_capacity_fails(self) -> None:
        bucket = TokenBucket(capacity=2, rate=0.001)
        assert bucket.consume() is True
        assert bucket.consume() is True
        assert bucket.consume() is False

    def test_near_zero_refill_does_not_restore_practically(self) -> None:
        """极低补充速率下，短窗口内不应恢复令牌。"""
        bucket = TokenBucket(capacity=1, rate=0.001)
        assert bucket.consume() is True
        time.sleep(0.05)
        assert bucket.consume() is False

    def test_refill_restores_tokens(self) -> None:
        bucket = TokenBucket(capacity=2, rate=100.0)
        assert bucket.consume() is True
        assert bucket.consume() is True
        assert bucket.consume() is False
        time.sleep(0.05)
        assert bucket.consume() is True

    def test_consume_amount_larger_than_capacity_fails(self) -> None:
        bucket = TokenBucket(capacity=5, rate=0.001)
        assert bucket.consume(6.0) is False

    def test_non_positive_rate_is_rejected(self) -> None:
        """rate 必须为正：零速率会让桶永久枯竭，属于配置错误。"""
        with pytest.raises(ValueError, match="rate"):
            TokenBucket(capacity=1, rate=0.0)

    def test_non_positive_capacity_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="capacity"):
            TokenBucket(capacity=0.0, rate=1.0)

    def test_available_never_exceeds_capacity(self) -> None:
        bucket = TokenBucket(capacity=2, rate=1000.0)
        time.sleep(0.02)
        assert bucket.available <= 2.0


class TestRateLimiter:
    """限流器按 key 独立计数。"""

    def test_allows_within_rate(self) -> None:
        limiter = RateLimiter(rate=10, capacity=10)
        assert limiter.allow("k1") is True
        assert limiter.allow("k1") is True

    def test_blocks_after_capacity(self) -> None:
        limiter = RateLimiter(rate=10, capacity=2)
        assert limiter.allow("k1") is True
        assert limiter.allow("k1") is True
        assert limiter.allow("k1") is False

    def test_keys_are_independent(self) -> None:
        limiter = RateLimiter(rate=10, capacity=1)
        assert limiter.allow("k1") is True
        assert limiter.allow("k2") is True
        assert limiter.allow("k1") is False

    def test_cleanup_removes_expired_entries(self) -> None:
        limiter = RateLimiter(rate=10, capacity=10, ttl_s=0.01)
        limiter.allow("k1")
        time.sleep(0.05)
        removed = limiter.cleanup()
        assert removed >= 1

    def test_cleanup_on_fresh_state_removes_nothing(self) -> None:
        limiter = RateLimiter(rate=10, capacity=10, ttl_s=3600)
        limiter.allow("k1")
        assert limiter.cleanup() == 0
