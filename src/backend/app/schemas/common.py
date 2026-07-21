"""通用 schema：垃圾箱批量操作等。"""

import uuid
from typing import Literal

from pydantic import BaseModel, Field


class TrashBatchAction(BaseModel):
    """批量：trash 移入垃圾箱 / restore 恢复 / delete 永久删除。"""

    action: Literal["trash", "restore", "delete"]
    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class BatchResult(BaseModel):
    affected: int
