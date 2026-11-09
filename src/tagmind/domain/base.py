"""领域模型基类与序列化助手。

设计约定：

- 领域实体一律 ``frozen=True`` 的 ``dataclass``：不可变 + 可哈希 + 可比较。
  不可变是刻意的——内容标签、画像权重这些值一旦算出就不该被旁路修改，
  否则「同一份 pipeline_version 必须产出同一份 labels」这条不变量会被破坏。
- 序列化助手把 ``Enum`` / ``tuple`` / ``set`` / ``datetime`` 统一转成
  JSON 友好的形态，供仓储层写 JSONB 时复用。
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from enum import Enum
from typing import Any, TypeAlias, TypeVar

__all__ = [
    "DomainModel",
    "JsonObject",
    "JsonValue",
    "to_jsonable",
]

JsonValue: TypeAlias = bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None
JsonObject: TypeAlias = dict[str, JsonValue]

_T = TypeVar("_T")


def to_jsonable(value: Any) -> JsonValue:
    """把任意领域值转成 JSON 友好的形态。

    转换规则：

    - ``Enum`` → ``.value``（``StrEnum`` 的 ``.value`` 就是字符串）
    - ``dataclass`` → ``dataclasses.asdict`` 后递归
    - ``tuple`` / ``set`` / ``frozenset`` → ``list``
    - ``datetime`` / ``date`` → ISO 8601 字符串
    - ``dict`` → 键转 ``str``（JSONB 的键必须是字符串）
    - 其余原样返回
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(dataclasses.asdict(value))
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_jsonable(item) for item in value]
    return str(value)


class DomainModel:
    """领域模型基类：提供统一序列化入口。

    子类通常是 ``@dataclass(frozen=True, slots=True)``。本基类**不是** dataclass，
    本身不持有任何字段，因此不会影响子类的字段顺序与冻结语义。
    """

    def to_dict(self) -> JsonObject:
        """转成 JSON 友好的字典。

        Returns:
            字段名 → JSON 值的字典。
        """
        if not dataclasses.is_dataclass(self):
            raise TypeError(f"{type(self).__name__} 不是 dataclass，无法序列化")
        return to_jsonable(dataclasses.asdict(self))

    def to_json(self) -> str:
        """转成 JSON 字符串。

        Returns:
            紧凑 JSON 字符串；``ensure_ascii=False`` 保证中文可读。
        """
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        """返回该 dataclass 的字段名元组。"""
        if not dataclasses.is_dataclass(cls):
            raise TypeError(f"{cls.__name__} 不是 dataclass")
        return tuple(field.name for field in dataclasses.fields(cls))
