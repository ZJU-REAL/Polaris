"""模型公共件：UUID 主键、时间戳 Mixin、JSON 列类型。"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

# 历史名字：以前在 postgres 上是 JSONB 变体，现在只有 SQLite，就是普通 JSON 列
JSONVariant = JSON()


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """时间列的唯一类型：库里存 UTC，读出来一律带 UTC 时区（#872）。

    SQLite 没有带时区的时间类型——SQLAlchemy 写入时直接丢掉 tzinfo、读出来是 naive
    值。naive 值序列化成 JSON 不带偏移，前端 ``new Date()`` 会按**本地**时间解析，
    东八区用户看到的时间就整整差 8 小时；和 ``utcnow()`` 比较还会抛 TypeError。

    - 写入：带时区的值先换算成 UTC（否则 +08:00 的值会按原钟面数字落库）；naive 值
      按 UTC 理解（与本类型出现之前的落库口径一致）。
    - 读出：补上 UTC 时区。

    落库格式没有变化（仍是 UTC 钟面时间），不需要数据迁移；比较、排序、区间过滤照旧。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False
    )
