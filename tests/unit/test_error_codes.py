"""A-15：把 ``ERROR_CODES`` 从死代码变成被测试锁定的契约。

``config/constants.py`` 的 ``ERROR_CODES`` 此前没有任何消费点：新增异常、
改错误码、拼错类名都不会被任何测试发现，而 API 统一错误体与文档生成都
依赖它与 ``core/errors.py`` 保持一致。
"""

from __future__ import annotations

import pytest

from tagmind.config.constants import ERROR_CODES
from tagmind.core import errors


@pytest.mark.parametrize("class_name,expected_code", sorted(ERROR_CODES.items()))
def test_registered_error_code_matches_exception_class(class_name: str, expected_code: str) -> None:
    """每个登记项都必须对应真实异常类，且类属性 code 完全一致。"""
    cls = getattr(errors, class_name, None)
    assert cls is not None, f"ERROR_CODES 登记了不存在的异常类 {class_name}"
    assert isinstance(cls, type)
    assert issubclass(cls, errors.TagmindError)
    assert cls.code == expected_code, f"{class_name}.code 与登记值不一致"


def test_every_defined_exception_is_registered() -> None:
    """反向核对：新加异常忘记登记也要能被发现。"""
    defined = {
        name
        for name, obj in vars(errors).items()
        if isinstance(obj, type)
        and issubclass(obj, errors.TagmindError)
        and obj.__module__ == errors.__name__
    }
    missing = defined - set(ERROR_CODES)
    assert not missing, f"这些异常类未在 ERROR_CODES 登记：{sorted(missing)}"


def test_error_codes_are_unique() -> None:
    """错误码不得重复，否则线上无法归因。"""
    codes = list(ERROR_CODES.values())
    assert len(codes) == len(set(codes))


def test_error_codes_follow_naming_convention() -> None:
    for class_name, code in ERROR_CODES.items():
        assert code.startswith("TM_"), f"{class_name} 的错误码 {code} 不符合 TM_XXXX 约定"
        assert len(code) == 7, f"{class_name} 的错误码 {code} 长度异常"
