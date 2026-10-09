"""敏感字段加密工具（Fernet）。

用于 SSH 私钥、服务器凭据、模型服务 key 等入库前加密；密钥来自 settings.encryption_key，生成：
``python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"``

桌面外壳首启时为每份安装生成独立的 POLARIS_SECRET_KEY 与 POLARIS_ENCRYPTION_KEY（#850）。
在那之前，引擎一直用公开的默认 secret_key 派生加密密钥——拿到库文件的人可以直接解开
里面的凭据。所以这里：

- 加密永远用当前主密钥；
- 解密依次试主密钥和「旧派生密钥」（公开默认 secret_key / 当前 secret_key 派生出来的），
  老数据照常读得出来；
- 启动时 :func:`rotate_encrypted_secrets` 把仍用旧密钥加密的值逐个换成主密钥加密，
  之后库里不再有能用公开默认值解开的密文。

没配 encryption_key（从源码跑、测试）时主密钥仍从 secret_key 派生，行为与以前一致。
"""

import base64
import copy
import hashlib
import logging
import uuid
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import LEGACY_DEFAULT_SECRET_KEY, get_settings

logger = logging.getLogger(__name__)


def _derive_key_from_secret(secret: str) -> bytes:
    """从任意字符串派生合法的 Fernet key（32 bytes urlsafe base64）。仅 dev 回退/旧数据解密用。"""
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _primary_key() -> bytes:
    settings = get_settings()
    if settings.encryption_key:
        return settings.encryption_key.encode("utf-8")
    if settings.env == "prod":
        raise RuntimeError("POLARIS_ENCRYPTION_KEY must be set in prod")
    return _derive_key_from_secret(settings.secret_key)


def _legacy_keys(primary: bytes) -> list[bytes]:
    """只用于解密的旧密钥：公开默认值派生的、当前 secret_key 派生的（去掉与主密钥相同的）。"""
    settings = get_settings()
    keys: list[bytes] = []
    for secret in (LEGACY_DEFAULT_SECRET_KEY, settings.secret_key):
        key = _derive_key_from_secret(secret)
        if key != primary and key not in keys:
            keys.append(key)
    return keys


@lru_cache
def get_fernet() -> MultiFernet:
    """加密用主密钥，解密依次试主密钥与旧派生密钥。

    主密钥格式不对时 ``Fernet(...)`` 抛 ValueError——那是部署配错，原样冒出去。
    """
    primary = _primary_key()
    return MultiFernet([Fernet(primary), *(Fernet(k) for k in _legacy_keys(primary))])


def _primary_fernet() -> Fernet:
    return Fernet(_primary_key())


def encrypt_secret(plaintext: str) -> str:
    """加密敏感字符串（如 SSH 私钥），返回可入库的 token。"""
    return get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    """解密 encrypt_secret 的输出（新旧密钥加密的都认）。"""
    return get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")


def needs_rotation(token: str) -> bool:
    """这个密文是不是还没用主密钥加密（主密钥解不开）。"""
    try:
        _primary_fernet().decrypt(token.encode("utf-8"))
        return False
    except InvalidToken:
        return True


def rotate_secret(token: str) -> str | None:
    """把一个旧密钥密文换成主密钥密文。已经是主密钥的、或谁都解不开的返回 None（不动它）。"""
    if not needs_rotation(token):
        return None
    try:
        return get_fernet().rotate(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None


# ---- 启动时的一次性轮换 ----


# 所有加密列：(模型, 列名)。新增加密列时务必登记到这里，否则它会一直停在旧密钥上。
def _encrypted_columns() -> list[tuple[Any, str]]:
    from app.models.acp_agent import AcpAgent
    from app.models.chat_bot import ChatBotConfig
    from app.models.llm_config import LLMProviderConfig
    from app.models.mcp_server import McpServer
    from app.models.ssh_credential import ConnectionCredential

    return [
        (ConnectionCredential, "private_key_encrypted"),
        (ConnectionCredential, "passphrase_encrypted"),
        (ConnectionCredential, "payload_encrypted"),
        (LLMProviderConfig, "api_key_encrypted"),
        (McpServer, "env_encrypted"),
        (AcpAgent, "env_encrypted"),
        (ChatBotConfig, "robot_id_encrypted"),
        (ChatBotConfig, "secret_encrypted"),
    ]


# system_settings 里装着加密凭据的文档：键 → 凭据列表所在字段与生成旧式条目 id 的命名空间前缀
_SETTING_CREDENTIAL_LISTS: dict[str, Callable[[Any], list[tuple[list[Any], str]]]] = {
    # literature_search.provider_keys = {source: [entry | 旧式裸密文字符串]}
    "literature_search": lambda v: [
        (items, f"polaris:{source}")
        for source, items in ((v or {}).get("provider_keys") or {}).items()
        if isinstance(items, list)
    ],
    # document_processing.mineru_credentials = [entry | 旧式裸密文字符串]
    "document_processing": lambda v: (
        [(v["mineru_credentials"], "polaris:mineru")]
        if isinstance(v, dict) and isinstance(v.get("mineru_credentials"), list)
        else []
    ),
}


def _rotate_credential_list(items: list[Any], id_prefix: str) -> bool:
    """就地轮换一个凭据列表，返回是否改动。

    旧式条目是裸密文字符串，读取时用「前缀:下标:密文」现算 uuid5 当 id；密文一换 id
    就变了，所以轮换时顺手把它展开成对象条目、钉住原来的 id。
    """
    changed = False
    for index, item in enumerate(items):
        if isinstance(item, str) and item:
            rotated = rotate_secret(item)
            if rotated is None:
                continue
            stable = uuid.uuid5(uuid.NAMESPACE_URL, f"{id_prefix}:{index}:{item}")
            items[index] = {
                "id": str(stable),
                "secret": rotated,
                "enabled": True,
                "label": None,
                "health": None,
                "created_at": None,
                "updated_at": None,
            }
            changed = True
        elif isinstance(item, dict) and isinstance(item.get("secret"), str):
            rotated = rotate_secret(item["secret"])
            if rotated is not None:
                item["secret"] = rotated
                changed = True
    return changed


async def rotate_encrypted_secrets(session: AsyncSession) -> int:
    """把库里仍用旧密钥加密的值全部换成主密钥加密，返回改动的值个数。

    - 幂等：主密钥已经能解开的值直接跳过，重复跑是空操作；
    - 中断安全：每张表单独提交，没轮到的值仍是旧密文，而解密同时认新旧密钥，
      任何时刻停下都不会有读不出来的数据，下次启动接着换；
    - 谁都解不开的值（密钥早就换过的坏数据）原样保留、只记日志，不在这里删用户数据。
    """
    from app.models.system_setting import SystemSetting

    if not _legacy_keys(_primary_key()):
        return 0  # 主密钥就是旧派生密钥（从源码跑、没配新密钥）：无事可做

    total = 0
    for model, column in _encrypted_columns():
        table = model.__table__
        col = table.c[column]
        pk = table.c["id"]
        rows = (await session.execute(select(pk, col).where(col.is_not(None)))).all()
        changed = 0
        for row_id, token in rows:
            if not token:
                continue
            rotated = rotate_secret(token)
            if rotated is None:
                if needs_rotation(token):
                    logger.warning(
                        "cannot decrypt %s.%s for row %s with any known key; left as is",
                        table.name,
                        column,
                        row_id,
                    )
                continue
            await session.execute(update(table).where(pk == row_id).values({column: rotated}))
            changed += 1
        if changed:
            await session.commit()
            total += changed

    for key, lists_of in _SETTING_CREDENTIAL_LISTS.items():
        row = await session.get(SystemSetting, key)
        if row is None or not isinstance(row.value, dict):
            continue
        value = copy.deepcopy(row.value)
        changed_any = False
        for items, id_prefix in lists_of(value):
            changed_any = _rotate_credential_list(items, id_prefix) or changed_any
        if changed_any:
            row.value = value
            await session.commit()
            total += 1

    if total:
        logger.info("re-encrypted %d stored secrets with the per-install key", total)
    return total
