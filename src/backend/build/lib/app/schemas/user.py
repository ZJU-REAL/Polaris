"""用户 schema（基于 fastapi-users），注册额外要求邀请码。"""

import uuid
from typing import Any

from fastapi_users import schemas


class UserRead(schemas.BaseUser[uuid.UUID]):
    display_name: str
    role: str


class UserCreate(schemas.BaseUserCreate):
    display_name: str = ""
    invite_code: str  # 与 settings.invite_code 比对，见 api/auth.py

    def create_update_dict(self) -> dict[str, Any]:
        d = super().create_update_dict()
        d.pop("invite_code", None)  # 非表字段，入库前剔除
        return d

    def create_update_dict_superuser(self) -> dict[str, Any]:
        d = super().create_update_dict_superuser()
        d.pop("invite_code", None)
        return d


class UserUpdate(schemas.BaseUserUpdate):
    display_name: str | None = None
