"""会话签名密钥：给本地会话 JWT 签名、验签用的那一把（HS256）。

来源按这个顺序定：

1. **配了 ``POLARIS_SECRET_KEY`` 且不短于 32 字节**——原样使用。桌面外壳为每份安装
   生成的就是这种（#850），行为与以前一致。
2. **配了但短于 32 字节**——不报错、不拒绝启动，也不忽略它：用它经 HMAC-SHA256 派生
   一把 64 字符的签名密钥。配置仍然决定密钥是什么（换配置就换密钥），只是不再把过短
   的值直接交给 HS256。派生不会凭空增加强度，短口令照样短——启动日志里会提醒换一把。
   升级到这个版本时，用旧的短密钥直接签出的会话会失效一次；前端收到 401 会自动重新
   取会话，用户无感。
3. **没配**（值还是公开的默认值，从源码跑 / 自己起容器的典型情况）——第一次启动时
   生成一把随机密钥，存到 ``<data_dir>/session-signing.key``（权限 0600），以后每次
   启动沿用它，所以重启不会让已签发的会话失效。删掉这个文件等于让所有会话重新取一次。

为什么要有第 3 条：默认值只有 29 字节，PyJWT 每次签名、验签都发一条
InsecureKeyLengthWarning。告警本身无害，但进程里任何地方把告警临时升级成异常时
（过滤器是整个进程共用的），正在验签的请求就会 500——见 services/vector_search.py
``_np_row`` 的说明。用够长的密钥，这条告警根本不会出现。

这里只管**会话签名**。``secret_key`` 的其它用途（没配 encryption_key 时派生加密密钥、
下载链接签名、浏览器扩展 key 摘要）仍然直接读配置值，不受影响——那些都是 hashlib /
Fernet 路径，没有长度告警，改它们的密钥来源还牵涉已入库密文的轮换，不在这里顺手做。
"""

import contextlib
import hashlib
import hmac
import logging
import os
import secrets
import threading
from pathlib import Path

from app.core.config import LEGACY_DEFAULT_SECRET_KEY, Settings, get_settings

logger = logging.getLogger("polaris.session_key")

#: RFC 7518 §3.2：HS256 的密钥不得短于散列输出（32 字节）。
MIN_KEY_BYTES = 32
#: 自动生成的密钥在 data_dir 下的文件名。
KEY_FILE_NAME = "session-signing.key"

_DERIVE_CONTEXT = b"polaris session signing key v1"

_lock = threading.Lock()
#: (secret_key, data_dir) → 签名密钥。按配置值缓存：用例改了配置不用另外清缓存。
_resolved: dict[tuple[str, str], str] = {}


def _long_enough(key: str) -> bool:
    return len(key.encode("utf-8")) >= MIN_KEY_BYTES


def _read_key(path: Path) -> str | None:
    try:
        key = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("会话签名密钥文件 %s 读不出来（%s），将重新生成", path, e)
        return None
    if not _long_enough(key):
        logger.warning("会话签名密钥文件 %s 内容不完整，将重新生成", path)
        return None
    return key


def load_or_create_key(data_dir: str | Path) -> str:
    """读 ``<data_dir>/session-signing.key``；没有（或内容不可用）就生成一把写进去。

    写入走「临时文件 + 原子替换」：别的进程要么看不到文件，要么看到完整的密钥，不会
    读到写了一半的内容。临时文件创建时就是 0600，密钥不会以更宽的权限出现在盘上。
    两个进程同时首次启动时各写各的、后写的生效，所以写完再读一遍，以盘上的为准。

    目录写不进去（只读挂载等）时不让引擎起不来：退回一把只在本进程内有效的随机密钥，
    代价是重启后会话要重新取一次。
    """
    path = Path(data_dir) / KEY_FILE_NAME
    existing = _read_key(path)
    if existing is not None:
        return existing
    key = secrets.token_urlsafe(48)
    tmp = path.with_name(f"{KEY_FILE_NAME}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key + "\n")
        os.replace(tmp, path)
    except OSError as e:
        with contextlib.suppress(OSError):  # 目录本身不可用时连清理都会失败
            tmp.unlink(missing_ok=True)
        logger.warning(
            "会话签名密钥写不进 %s（%s）：本次运行改用临时密钥，重启后需要重新取会话", path, e
        )
        return key
    logger.info("已生成会话签名密钥：%s", path)
    return _read_key(path) or key


def derive_from_short_secret(secret: str) -> str:
    """把过短的配置值派生成够长的签名密钥（确定性：同一个配置值总得到同一把）。"""
    return hmac.new(secret.encode("utf-8"), _DERIVE_CONTEXT, hashlib.sha256).hexdigest()


def session_signing_key(settings: Settings | None = None) -> str:
    """当前配置下的会话签名密钥（规则见模块说明）。"""
    settings = settings or get_settings()
    secret = settings.secret_key
    cache_key = (secret, settings.data_dir)
    with _lock:
        cached = _resolved.get(cache_key)
        if cached is not None:
            return cached
        if not secret or secret == LEGACY_DEFAULT_SECRET_KEY:
            key = load_or_create_key(settings.data_dir)
        elif _long_enough(secret):
            key = secret
        else:
            logger.warning(
                "POLARIS_SECRET_KEY 只有 %d 字节，短于会话签名要求的 %d 字节：已改用由它派生的"
                "签名密钥。建议换成更长的随机值（openssl rand -hex 32）",
                len(secret.encode("utf-8")),
                MIN_KEY_BYTES,
            )
            key = derive_from_short_secret(secret)
        _resolved[cache_key] = key
        return key


def reset_session_signing_key_cache() -> None:
    """丢掉已解析的密钥（测试模拟「重启」用）。"""
    with _lock:
        _resolved.clear()
