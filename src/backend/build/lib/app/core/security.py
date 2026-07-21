"""敏感字段加密工具（Fernet）。

用于 SSH 私钥、服务器凭据等入库前加密；密钥来自 settings.encryption_key
（``python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"``）。
dev 环境未配置时从 secret_key 派生一个确定性密钥，生产必须显式配置。
"""

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet

from app.core.config import get_settings


def _derive_key_from_secret(secret: str) -> bytes:
    """从任意字符串派生合法的 Fernet key（32 bytes urlsafe base64）。仅 dev 回退用。"""
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


@lru_cache
def get_fernet() -> Fernet:
    settings = get_settings()
    if settings.encryption_key:
        return Fernet(settings.encryption_key.encode("utf-8"))
    if settings.env == "prod":
        raise RuntimeError("POLARIS_ENCRYPTION_KEY must be set in prod")
    return Fernet(_derive_key_from_secret(settings.secret_key))


def encrypt_secret(plaintext: str) -> str:
    """加密敏感字符串（如 SSH 私钥），返回可入库的 token。"""
    return get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    """解密 encrypt_secret 的输出。"""
    return get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
