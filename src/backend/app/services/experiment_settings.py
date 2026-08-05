"""实验的全局设置（system_settings 里一个 key 存一份，所有实验共用）。

为什么做成全局一份而不是挂在 SSH 凭据上：模型/数据集放哪、用哪个 pip 镜像，实验室里
是统一约定的，一处配好处处生效比每台机器各填一遍省事，也不会漏配。机器之间真有差异
时再谈按机器覆盖（凭据上已有 proxy_url 是那条路的先例）。

**这些值会拼进远端 shell 的 export**，所以每个字段都必须过白名单校验——校验失败即拒收，
绝不把没校验过的字符串写进 env.sh。
"""

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_setting import SystemSetting

SETTING_KEY = "experiment_env"

# 主机路径：绝对或 ~ 开头，禁 shell 元字符与 ..（与 ssh_exec.validate_host_path 同口径）
_PATH_RE = re.compile(r"^[~/][A-Za-z0-9._/~@-]*$")
# http(s) URL；允许带路径（pip 镜像形如 https://pypi.tuna.tsinghua.edu.cn/simple）
_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d+)?(/[A-Za-z0-9._~/\-]*)?$")


class InvalidExperimentSettingError(ValueError):
    """某个字段没过校验。``field`` 指出是哪一个，便于前端定位。"""

    def __init__(self, field: str, value: str) -> None:
        super().__init__(f"{field}={value!r}")
        self.field = field
        self.value = value


#: 字段 → 校验正则。留空一律合法（= 不配置该项，走各自的兜底行为）。
_FIELDS: dict[str, re.Pattern[str]] = {
    "model_root": _PATH_RE,  # 本机模型根目录，如 /hf/model
    "dataset_root": _PATH_RE,  # 本机数据集根目录
    "pip_index_url": _URL_RE,  # pip 镜像源
    "hf_endpoint": _URL_RE,  # HF 镜像端点，如 https://hf-mirror.com
    "proxy_url": _URL_RE,  # 实验机出外网的 HTTP 代理
}

DEFAULTS: dict[str, str] = {
    "model_root": "",
    "dataset_root": "",
    "pip_index_url": "",
    "hf_endpoint": "",
    "proxy_url": "",
}


def _clean(data: Any) -> dict[str, str]:
    """把存量/入参规范成「只含已知字段的字符串字典」，未知键丢弃。"""
    out = dict(DEFAULTS)
    if isinstance(data, dict):
        for field in _FIELDS:
            raw = data.get(field)
            if raw is None:
                continue
            out[field] = str(raw).strip()
    return out


def validate(data: Any) -> dict[str, str]:
    """校验并规范化。空值 = 不配置，合法；非空则必须过该字段的白名单。"""
    cleaned = _clean(data)
    for field, pattern in _FIELDS.items():
        value = cleaned[field]
        if value and not pattern.match(value):
            raise InvalidExperimentSettingError(field, value)
        if value and ".." in value:  # 路径穿越，正则允许 . 但不该出现 ..
            raise InvalidExperimentSettingError(field, value)
    return cleaned


async def get_settings(session: AsyncSession) -> dict[str, str]:
    """读当前实验设置；没配过或存量值损坏都回落默认（空），绝不抛。"""
    row = await session.get(SystemSetting, SETTING_KEY)
    return _clean(row.value if row is not None else None)


async def set_settings(session: AsyncSession, data: Any) -> dict[str, str]:
    """校验后整份覆盖写入。字段非法抛 InvalidExperimentSettingError。"""
    cleaned = validate(data)
    row = await session.get(SystemSetting, SETTING_KEY)
    if row is None:
        session.add(SystemSetting(key=SETTING_KEY, value=cleaned))
    else:
        row.value = cleaned
    await session.commit()
    return cleaned
