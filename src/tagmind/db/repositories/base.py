"""仓储层：Protocol 基类、事务边界、乐观锁重试。

架构契约（§14.5）：

- **一个仓储方法 = 一个事务**。禁止在一个事务里做 LLM / HTTP 调用。
- **编排层持有跨仓储事务**。跨多个仓储的业务操作由编排层用
  ``Session`` 上下文管理器显式包裹。
- **唯一约束冲突视为幂等重放**，不重试，直接返回既有行（ADR-0009）。
- **死锁指数退避重试 ≤ 3 次**。

幂等的**最终保证在数据库唯一约束**，不在应用层的 SELECT-then-INSERT，也不在
Redis checkpoint。因此仓储提供 ``insert_ignore`` 语义，让唯一约束冲突变成
「返回既有行」而不是异常。
"""

from __future__ import annotations

import builtins
import time
from collections.abc import Callable, Sequence
from typing import Any, Generic, Protocol, TypeVar, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from tagmind.core.errors import DatabaseError, OptimisticLockError

__all__ = [
    "BaseRepository",
    "FilterSpec",
    "Repository",
    "bulk_delete",
    "retry_on_deadlock",
]

M = TypeVar("M")
T = TypeVar("T")


class FilterSpec:
    """过滤条件描述。

    用 ``dict`` 而不是一堆关键字参数，是因为不同实体的过滤维度差异很大
    （内容按状态/标签，标签按版本/层，画像按冷启动标记……），强行统一签名
    只会得到一个谁都不适用的 ``**kwargs``。
    """

    def __init__(self, **conditions: Any) -> None:
        """初始化过滤条件。

        Args:
            conditions: 列名 → 过滤值。``None`` 值表示忽略该列。
        """
        self.conditions = {k: v for k, v in conditions.items() if v is not None}

    def __repr__(self) -> str:
        """可读的调试表示。"""
        return f"FilterSpec({self.conditions!r})"

    def __bool__(self) -> bool:
        """是否有任何过滤条件。"""
        return bool(self.conditions)


class Repository(Protocol, Generic[M]):
    """所有仓储的统一约定。"""

    def get(self, entity_id: int) -> M | None:
        """按主键取单条。"""
        ...

    def list(self, filters: FilterSpec, *, limit: int, offset: int) -> Sequence[M]:
        """按条件分页取多条。"""
        ...

    def upsert(self, entity: M) -> M:
        """插入或更新一条，返回落库后的实体。"""
        ...

    def delete(self, entity_id: int) -> bool:
        """删除一条，返回是否真的删掉了。"""
        ...


def retry_on_deadlock(
    operation: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay_s: float = 0.05,
) -> T:
    """在死锁 / 序列化失败时指数退避重试。

    PostgreSQL 的死锁检测会让其中一方拿到 ``DeadlockDetected`` 错误；此时
    退避后重试通常就能成功。超过上限则抛出 :class:`DatabaseError`。

    Args:
        operation: 待执行的操作（通常是仓储方法）。
        max_attempts: 最大尝试次数。
        base_delay_s: 退避基数（秒），实际延迟为 ``base × 2^(n-1)``。

    Returns:
        操作的返回值。

    Raises:
        DatabaseError: 重试次数耗尽仍失败。
    """
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return operation()
        except OperationalError as exc:
            if not _is_retryable(exc):
                raise
            last_error = exc
            if attempt == max_attempts - 1:
                break
            time.sleep(base_delay_s * (2**attempt))
    raise DatabaseError(
        "数据库操作因死锁或序列化失败重试耗尽",
        details={"attempts": max_attempts, "error": str(last_error)},
    ) from last_error


def _is_retryable(exc: OperationalError) -> bool:
    """判断 ``OperationalError`` 是否值得重试。

    只重试「并发争用」类错误；连接失效、权限问题等重试无意义。
    """
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return sqlstate in {"40001", "40P01"}


class BaseRepository(Generic[M]):
    """仓储基类：提供事务边界、乐观锁与幂等插入的通用实现。

    子类必须提供：

    - ``model``：ORM 模型类。
    - ``_map_row_to_domain(row)``：ORM 行 → 领域对象。
    - ``_map_domain_to_values(entity)``：领域对象 → 列值字典。
    """

    #: ORM 模型类（由子类覆盖）。
    model: type[Any]

    def __init__(self, session: Session) -> None:
        """初始化仓储。

        Args:
            session: 事务内的 Session。由调用方持有事务边界。
        """
        self._session = session

    @property
    def session(self) -> Session:
        """当前 Session。"""
        return self._session

    # --- 子类必须实现 -------------------------------------------------

    def _map_row_to_domain(self, row: Any) -> M:
        """ORM 行 → 领域对象。"""
        raise NotImplementedError

    def _map_domain_to_values(self, entity: M) -> dict[str, Any]:
        """领域对象 → 列值字典。"""
        raise NotImplementedError

    # --- 通用实现 -----------------------------------------------------

    def get(self, entity_id: int) -> M | None:
        """按主键取单条。

        Args:
            entity_id: 主键值。

        Returns:
            领域对象；不存在时返回 ``None``。
        """
        row = self._session.get(self.model, entity_id)
        return self._map_row_to_domain(row) if row is not None else None

    def list(
        self,
        filters: FilterSpec | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
        order_by: Any = None,
    ) -> Sequence[M]:
        """按条件分页取多条。

        Args:
            filters: 过滤条件；``None`` 表示不过滤。
            limit: 返回条数上限。
            offset: 偏移量。
            order_by: 排序表达式；``None`` 时用模型主键排序。

        Returns:
            领域对象序列。
        """
        stmt = select(self.model)
        for column, value in (filters.conditions if filters else {}).items():
            stmt = stmt.where(getattr(self.model, column) == value)
        stmt = stmt.order_by(order_by if order_by is not None else self.model.id)
        stmt = stmt.limit(limit).offset(offset)
        rows: Sequence[Any] = self._session.execute(stmt).scalars().all()
        return [self._map_row_to_domain(row) for row in rows]

    def count(self, filters: FilterSpec | None = None) -> int:
        """按条件计数。

        Args:
            filters: 过滤条件。

        Returns:
            符合条件的行数。
        """
        stmt = select(func.count()).select_from(self.model)
        for column, value in (filters.conditions if filters else {}).items():
            stmt = stmt.where(getattr(self.model, column) == value)
        return int(self._session.execute(stmt).scalar_one())

    def upsert(self, entity: M) -> M:
        """插入或更新一条。

        ``created_at`` / ``updated_at`` 这类由数据库维护的列，若领域对象
        没有值（``None``），**不写入**——否则会用 ``NULL`` 覆盖掉
        ``server_default``（``now()``），直接违反 NOT NULL 约束。

        Args:
            entity: 领域对象。

        Returns:
            落库后的领域对象。
        """
        values = self._map_domain_to_values(entity)
        primary_key = self._primary_key_name()
        entity_id = values.pop(primary_key, None)
        # 领域对象用 id=0 表示「尚未落库」——它不是合法主键，必须走 INSERT。
        # 判据用 ``<= 0`` 而不是 ``is None``：新建对象的默认 id 就是 0。
        if entity_id is not None and entity_id <= 0:
            entity_id = None
        values = self._drop_server_managed(values)
        if entity_id is None:
            row = self.model(**values)
            self._session.add(row)
            self._session.flush()
            return self._map_row_to_domain(row)
        existing = self._session.get(self.model, entity_id)
        if existing is None:
            row = self.model(id=entity_id, **values)
            self._session.add(row)
            self._session.flush()
            return self._map_row_to_domain(row)
        for column, value in values.items():
            setattr(existing, column, value)
        self._session.flush()
        return self._map_row_to_domain(existing)

    def _drop_server_managed(self, values: dict[str, Any]) -> dict[str, Any]:
        """剔除「由数据库维护」的列上的 ``None``。

        判据：列有 ``server_default`` 且值为 ``None`` → 剔除。
        这样 ``created_at`` 走 ``now()``，``updated_at`` 走触发器，
        而业务上确实要写 ``NULL`` 的列（如 ``published_at``，没有 server_default）
        不受影响。
        """
        table = self.model.__table__
        return {
            column: value
            for column, value in values.items()
            if not (
                value is None
                and column in table.columns
                and table.columns[column].server_default is not None
            )
        }

    def delete(self, entity_id: int) -> bool:
        """删除一条。

        Args:
            entity_id: 主键值。

        Returns:
            确实删除了一行返回 ``True``；原本就不存在返回 ``False``。
        """
        row = self._session.get(self.model, entity_id)
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    def insert_ignore(self, entity: M) -> tuple[M, bool]:
        """幂等插入：唯一约束冲突时返回既有行。

        这是「幂等最终保证在 DB 唯一约束」的应用层表达——**不**做
        SELECT-then-INSERT 的检查（那有竞态窗口）。

        为避免唯一冲突把整个外层事务回滚掉，这里用 SAVEPOINT 隔离单次插入。

        Args:
            entity: 待插入的领域对象。

        Returns:
            ``(落库后的实体, 是否真的新插入)``。

        Raises:
            DatabaseError: 非唯一约束类别的完整性错误。
        """
        values = self._map_domain_to_values(entity)
        values.pop(self._primary_key_name(), None)
        values = self._drop_server_managed(values)
        savepoint = self._session.begin_nested()
        row = self.model(**values)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError:
            savepoint.rollback()
            existing = self._find_by_unique(values)
            if existing is None:
                raise
            return existing, False
        savepoint.commit()
        return self._map_row_to_domain(row), True

    def _find_by_unique(self, values: dict[str, Any]) -> M | None:
        """按唯一键回查既有行。

        Args:
            values: 插入尝试用的列值。

        Returns:
            找到的领域对象；未找到返回 ``None``。
        """
        unique_columns = self._unique_column_names()
        candidates = [col for col in unique_columns if col in values and values[col] is not None]
        if not candidates:
            return None
        stmt = select(self.model)
        for column in candidates:
            stmt = stmt.where(getattr(self.model, column) == values[column])
        row = self._session.execute(stmt.limit(1)).scalars().first()
        return self._map_row_to_domain(row) if row is not None else None

    def _unique_column_names(self) -> builtins.list[str]:
        """返回模型上所有单列唯一约束的列名。"""
        names: builtins.list[str] = []
        for constraint in self.model.__table__.constraints:
            if constraint.__class__.__name__ != "UniqueConstraint":
                continue
            columns = builtins.list(constraint.columns)
            if len(columns) == 1:
                names.append(columns[0].name)
        return names

    def optimistic_update(
        self,
        entity_id: int,
        values: dict[str, Any],
        *,
        expected_version_column: str = "updated_at",
        expected_version: Any = None,
    ) -> bool:
        """带版本条件的更新（乐观锁）。

        条件更新未命中说明别人已经改过这行，此时**不**重试，交由调用方决定
        是重读后重试还是放弃——盲目重试会覆盖别人的修改。

        ``expected_version`` 必须是调用方**先前读到**的那个版本值（通常是
        它读出的 ``updated_at``）。这里**不能**自己去读当前值——那样读到的
        永远是最新值，条件永远成立，乐观锁就成了摆设。

        Args:
            entity_id: 主键值。
            values: 要更新的列值。
            expected_version_column: 版本列名（默认 ``updated_at``）。
            expected_version: 期望的版本值；``None`` 时读当前值（**不**用于
                并发场景，仅用于「无并发假设」的普通更新）。

        Returns:
            更新成功返回 ``True``；版本不匹配返回 ``False``。

        Raises:
            OptimisticLockError: 行不存在。
        """
        version_column = getattr(self.model, expected_version_column)
        if expected_version is None:
            row = self._session.execute(
                select(version_column).where(self.model.id == entity_id)
            ).first()
            if row is None:
                raise OptimisticLockError(
                    "乐观锁更新失败：目标行不存在",
                    details={"entity_id": entity_id},
                )
            expected_version = row[0]
        stmt = (
            update(self.model)
            .where(self.model.id == entity_id)
            .where(version_column == expected_version)
        )
        for column, value in values.items():
            stmt = stmt.values(**{column: value})
        result = cast("CursorResult[Any]", self._session.execute(stmt))
        self._session.flush()
        return result.rowcount > 0

    def _primary_key_name(self) -> str:
        """返回主键列名。"""
        primary_key = builtins.list(self.model.__table__.primary_key.columns)
        if len(primary_key) != 1:
            raise DatabaseError(
                "仓储基类只支持单列主键",
                details={"model": self.model.__name__, "pk_count": len(primary_key)},
            )
        return str(primary_key[0].name)


def bulk_delete(model: type[Any], session: Session, entity_ids: Sequence[int]) -> int:
    """批量删除。

    Args:
        model: ORM 模型类。
        session: Session。
        entity_ids: 主键列表。

    Returns:
        删除的行数。
    """
    if not entity_ids:
        return 0
    result = cast(
        "CursorResult[Any]", session.execute(delete(model).where(model.id.in_(entity_ids)))
    )
    session.flush()
    return int(result.rowcount or 0)
