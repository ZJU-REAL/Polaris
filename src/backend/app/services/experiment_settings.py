"""实验的全局设置（system_settings 里一个 key 存一份，所有实验共用）。

留在 system_settings 而不迁用户偏好（#737 分层）：这些值描述的是**实验机**的
路径/镜像/代理约定——是部署级机器事实，不是谁的口味；换个用户登录，模型还是
放在同一个目录里。

为什么做成全局一份而不是挂在 SSH 凭据上：模型/数据集放哪、用哪个 pip 镜像，实验室里
是统一约定的，一处配好处处生效比每台机器各填一遍省事，也不会漏配。机器之间真有差异
时再谈按机器覆盖（凭据上已有 proxy_url 是那条路的先例）。

**这些值会拼进远端 shell 的 export**，所以每个字段都必须过白名单校验——校验失败即拒收，
绝不把没校验过的字符串写进 env.sh。

固定字段偏向 Python/机器学习（模型目录、pip、HF），别的领域用不上；``env_vars`` 是
通用的出口：管理员自己列 NAME=VALUE，每个实验都导出。名字过白名单、不许覆盖平台自己
导出的那几个；值在写 env.sh 时整体 shlex.quote，所以值里可以有空格等任意可见字符，
但不许有换行/控制字符。
"""

import re
from typing import Any, TypedDict

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


class EnvVar(TypedDict):
    name: str
    value: str


# 自定义环境变量名：shell 标识符
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# 平台自己导出或 shell 离不开的名字：自定义变量不许覆盖（大小写不敏感比较）
RESERVED_ENV_NAMES = frozenset(
    name.upper()
    for name in (
        "POLARIS_WORKDIR",
        "POLARIS_MODEL_ROOT",
        "POLARIS_DATASET_ROOT",
        "PIP_INDEX_URL",
        "HF_ENDPOINT",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "PATH",
        "HOME",
        "SHELL",
        "USER",
        "PWD",
        "VIRTUAL_ENV",
    )
)
MAX_ENV_VARS = 50
MAX_ENV_VALUE_LEN = 2000

DEFAULTS: dict[str, Any] = {
    "model_root": "",
    "dataset_root": "",
    "pip_index_url": "",
    "hf_endpoint": "",
    "proxy_url": "",
    "env_vars": [],
}


def _clean_env_vars(raw: Any) -> list[EnvVar]:
    """规范成 [{name, value}]；名字为空的行丢弃（前端留的空行），其余原样交给校验。"""
    out: list[EnvVar] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        value = item.get("value")
        out.append({"name": name, "value": "" if value is None else str(value)})
    return out


def _clean(data: Any) -> dict[str, Any]:
    """把存量/入参规范成「只含已知字段」的字典，未知键丢弃。"""
    out = dict(DEFAULTS)
    out["env_vars"] = []
    if isinstance(data, dict):
        for field in _FIELDS:
            raw = data.get(field)
            if raw is None:
                continue
            out[field] = str(raw).strip()
        out["env_vars"] = _clean_env_vars(data.get("env_vars"))
    return out


def _check_env_vars(env_vars: list[EnvVar]) -> None:
    if len(env_vars) > MAX_ENV_VARS:
        raise InvalidExperimentSettingError("env_vars", f"{len(env_vars)} entries")
    seen: set[str] = set()
    for var in env_vars:
        name, value = var["name"], var["value"]
        if not _ENV_NAME_RE.match(name) or name.upper() in RESERVED_ENV_NAMES:
            raise InvalidExperimentSettingError("env_vars", name)
        if name in seen:
            raise InvalidExperimentSettingError("env_vars", name)
        seen.add(name)
        # 值会被 shlex.quote，但换行/控制字符会把 env.sh 拆成多行，一律拒收
        if len(value) > MAX_ENV_VALUE_LEN or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise InvalidExperimentSettingError("env_vars", name)


def validate(data: Any) -> dict[str, Any]:
    """校验并规范化。空值 = 不配置，合法；非空则必须过该字段的白名单。"""
    cleaned = _clean(data)
    for field, pattern in _FIELDS.items():
        value = cleaned[field]
        if value and not pattern.match(value):
            raise InvalidExperimentSettingError(field, value)
        if value and ".." in value:  # 路径穿越，正则允许 . 但不该出现 ..
            raise InvalidExperimentSettingError(field, value)
    _check_env_vars(cleaned["env_vars"])
    return cleaned


async def get_settings(session: AsyncSession) -> dict[str, Any]:
    """读当前实验设置；没配过或存量值损坏都回落默认（空），绝不抛。"""
    row = await session.get(SystemSetting, SETTING_KEY)
    return _clean(row.value if row is not None else None)


async def set_settings(session: AsyncSession, data: Any) -> dict[str, Any]:
    """校验后整份覆盖写入。字段非法抛 InvalidExperimentSettingError。"""
    cleaned = validate(data)
    row = await session.get(SystemSetting, SETTING_KEY)
    if row is None:
        session.add(SystemSetting(key=SETTING_KEY, value=cleaned))
    else:
        row.value = cleaned
    await session.commit()
    return cleaned
