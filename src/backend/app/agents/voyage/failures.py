"""任务失败的归类与记录：一个失败是「等一会儿再试就可能好」还是「得有人来处理」。

引擎把任务停在 ``paused_error`` 时，往 ``checkpoint["last_error"]`` 里存一条记录：

    {code, message, detail, transient, source, at, step_id?, step_title?}

- ``code``：稳定的错误码，界面按它给文案（见 :data:`MESSAGES`）；
- ``message``：给人看的一句话；``detail``：原始报错，排查用；
- ``transient``：是否像临时性故障（网络、超时、限流、对方服务故障、本地库正忙）。

归类只有 :func:`classify_failure` 这一处。新增规则加在这里，不要在调用方另写判断。
"""

from __future__ import annotations

import errno
import json
import socket
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.agents.voyage.errorsig import error_text
from app.core.errors import PreconditionError
from app.models.base import utcnow


@dataclass(frozen=True, slots=True)
class FailureClass:
    code: str
    transient: bool
    #: 异常自带的给人看的说明（类型化的模型错误才有）
    user_message: str | None = None


# 错误码 → 给人看的一句话。引擎没有界面语言的概念，这里是兜底文案；
# 界面拿 code 自己出中英文。
MESSAGES: dict[str, str] = {
    "NETWORK": "网络连接失败",
    "TIMEOUT": "请求超时",
    "RATE_LIMITED": "请求太频繁，被对方服务暂时限流",
    "UPSTREAM_UNAVAILABLE": "对方服务暂时不可用",
    "DB_BUSY": "本地数据库正忙",
    "AUTH": "对方服务拒绝了访问（密钥或登录状态有问题）",
    "BAD_REQUEST": "对方服务不接受这次请求",
    "NOT_CONFIGURED": "还没有配置可用的模型",
    "REFUSAL": "模型拒绝了这次请求",
    "INVALID_OUTPUT": "拿到的结果不符合要求",
    "MISSING_PREREQUISITE": "缺少运行所需的文件或组件",
    "INTERNAL": "程序内部出错",
    "STEP_FAILED": "这一步执行失败",
    "CHECK_FAILED": "这一步的结果没有通过检查",
    "NEEDS_USER": "需要你来决定下一步",
    "NO_PROGRESS": "多次自动恢复后仍然没有进展",
}

# 出错位置 → 句子开头
_WHERE: dict[str, str] = {
    "planner": "规划任务时出错",
    "verifier": "检查步骤结果时出错",
    "action": "执行步骤时出错",
    "driver": "任务运行时出错",
    "reconcile": "任务已暂停",
}

_NETWORK_ERRNOS = frozenset(
    {
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.ENETDOWN,
        errno.EPIPE,
    }
)
_TRANSIENT_STATUSES = frozenset({408, 409, 425})


def _mro_names(exc: BaseException) -> tuple[str, ...]:
    return tuple(cls.__name__ for cls in type(exc).__mro__)


def _has_name(names: tuple[str, ...], *needles: str) -> bool:
    return any(needle in name for name in names for needle in needles)


def _http_status(exc: BaseException) -> int | None:
    """从各家 HTTP 客户端的异常上取状态码（httpx / SDK 的 status_code、aiohttp 的 status）。"""
    for owner in (exc, getattr(exc, "response", None)):
        if owner is None:
            continue
        for attr in ("status_code", "status"):
            value = getattr(owner, attr, None)
            if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
                return value
    return None


def _strong(exc: BaseException) -> FailureClass | None:
    """这个异常本身带着明确信号时给出归类；只是个普通 ValueError 之类则返回 None。"""
    # 1. 类型化的错误（模型错误、PreconditionError）自己声明了 transient：以它为准
    declared = getattr(exc, "transient", None)
    if isinstance(declared, bool):
        code = getattr(exc, "code", None)
        if not isinstance(code, str) or not code:
            code = "UPSTREAM_UNAVAILABLE" if declared else "BAD_REQUEST"
        user_message = getattr(exc, "user_message", None)
        return FailureClass(
            code, declared, user_message if isinstance(user_message, str) and user_message else None
        )

    names = _mro_names(exc)
    # 2. 按类名认（不 import 各家 SDK：装没装、哪个版本都不影响）
    if _has_name(names, "Refusal"):
        return FailureClass("REFUSAL", False)
    if _has_name(names, "NotConfigured"):
        return FailureClass("NOT_CONFIGURED", False)
    if _has_name(names, "AuthenticationError", "PermissionDenied", "Unauthorized"):
        return FailureClass("AUTH", False)
    if _has_name(names, "RateLimit"):
        return FailureClass("RATE_LIMITED", True)
    if _has_name(names, "Overloaded"):
        return FailureClass("UPSTREAM_UNAVAILABLE", True)

    # 3. HTTP 状态码
    status = _http_status(exc)
    if status is not None:
        if status == 429:
            return FailureClass("RATE_LIMITED", True)
        if status in (401, 403):
            return FailureClass("AUTH", False)
        if status == 408:
            return FailureClass("TIMEOUT", True)
        if status >= 500 or status in _TRANSIENT_STATUSES:
            return FailureClass("UPSTREAM_UNAVAILABLE", True)
        if status >= 400:
            return FailureClass("BAD_REQUEST", False)

    # 4. 超时（asyncio.TimeoutError 在 3.11+ 就是内置 TimeoutError；httpx 的靠类名）
    if isinstance(exc, TimeoutError) or _has_name(names, "Timeout"):
        return FailureClass("TIMEOUT", True)

    # 5. 网络
    if isinstance(exc, ConnectionError | socket.gaierror) or _has_name(
        names,
        "TransportError",  # httpx 的连接/读写/协议错误共同基类
        "APIConnectionError",
        "ClientConnectionError",
        "ConnectionLost",
    ):
        return FailureClass("NETWORK", True)
    if isinstance(exc, OSError):
        if exc.errno == errno.ETIMEDOUT:
            return FailureClass("TIMEOUT", True)
        if exc.errno in _NETWORK_ERRNOS:
            return FailureClass("NETWORK", True)

    # 6. SQLite 被占用：另一个写入在进行，等它结束即可
    if isinstance(exc, sqlite3.OperationalError) or _has_name(names, "OperationalError"):
        text = str(exc).lower()
        if "database is locked" in text or "database is busy" in text:
            return FailureClass("DB_BUSY", True)
    return None


def _weak(exc: BaseException) -> FailureClass:
    """没有明确信号时按异常类型兜底——一律当作需要人处理。"""
    names = _mro_names(exc)
    if isinstance(exc, FileNotFoundError | ImportError):
        return FailureClass("MISSING_PREREQUISITE", False)
    if isinstance(exc, ValueError | json.JSONDecodeError) or _has_name(names, "ValidationError"):
        return FailureClass("INVALID_OUTPUT", False)
    return FailureClass("INTERNAL", False)


def _chain(exc: BaseException) -> list[BaseException]:
    """异常及其起因链（``raise ... from e`` 与处理另一个异常时抛出的都算）。"""
    out: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(out) < 8:
        seen.add(id(current))
        out.append(current)
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return out


def classify_failure(exc: BaseException) -> FailureClass:
    """把一个异常归成「临时性的」或「需要人处理的」，并给出错误码。

    顺序：先看异常自己声明的 ``transient``（类型化的模型错误），再按类名、HTTP 状态码、
    超时/网络类型、errno、SQLite 占用判断。外层常是一个包了一层的普通异常
    （``raise ValueError(...) from 超时``），所以沿起因链找第一个带明确信号的；
    整条链都没有信号才按最外层的类型兜底，兜底一律不算临时性。
    """
    for item in _chain(exc):
        found = _strong(item)
        if found is not None:
            return found
    return _weak(exc)


def error_record(
    code: str,
    *,
    transient: bool,
    source: str,
    detail: str = "",
    message: str | None = None,
    step_id: Any = None,
    step_title: str | None = None,
) -> dict[str, Any]:
    """组装一条 ``checkpoint["last_error"]`` 记录（全是 JSON 可存的值）。"""
    reason = message or MESSAGES.get(code) or MESSAGES["INTERNAL"]
    where = _WHERE.get(source)
    record: dict[str, Any] = {
        "code": code,
        "message": f"{where}：{reason}" if where else reason,
        "detail": detail[:2000],
        "transient": bool(transient),
        "source": source,
        "at": utcnow().isoformat(),
    }
    if step_id is not None:
        record["step_id"] = str(step_id)
    if step_title:
        record["step_title"] = step_title
    return record


# ---- 临时性故障的自动重试 ----
#
# 停在 paused_error 的任务，原因是临时性的就自己过一会儿再试：第 1、5、20 分钟各一次，
# 三次都不行才真的停着等人。状态存在 checkpoint["auto_retry"]：
#
#     {"attempt": 已排到第几次（1..3）, "next_at": 下一次的时刻（ISO）| None}
#
# 存在库里而不是内存里的定时器：引擎重启后定时任务照样能看到到点的那一条。

AUTO_RETRY_DELAYS_MINUTES: tuple[int, ...] = (1, 5, 20)


def schedule_auto_retry(
    checkpoint: dict[str, Any], error: dict[str, Any], *, now: datetime | None = None
) -> None:
    """任务刚因 ``error`` 停下来：临时性故障且次数没用完就排下一次，否则不排。

    原地改 ``checkpoint``。不是临时性的也要把挂着的 ``next_at`` 清掉——上一次是网络
    问题、这一次是密钥错了，就不该再自己试了。
    """
    state = checkpoint.get("auto_retry")
    state = state if isinstance(state, dict) else {}
    attempt = int(state.get("attempt", 0) or 0)
    if error.get("transient") is True and attempt < len(AUTO_RETRY_DELAYS_MINUTES):
        due = (now or utcnow()) + timedelta(minutes=AUTO_RETRY_DELAYS_MINUTES[attempt])
        checkpoint["auto_retry"] = {"attempt": attempt + 1, "next_at": due.isoformat()}
    elif state:
        checkpoint["auto_retry"] = {"attempt": attempt, "next_at": None}


def clear_auto_retry(checkpoint: dict[str, Any]) -> bool:
    """人接手了（取消、手动重试）或任务往前走了：自动重试的计数作废。返回是否改了。"""
    return checkpoint.pop("auto_retry", None) is not None


def auto_retry_due_at(checkpoint: dict[str, Any] | None) -> datetime | None:
    """排好的下一次自动重试时刻；没排（或数据坏了）返回 None。"""
    state = (checkpoint or {}).get("auto_retry")
    raw = state.get("next_at") if isinstance(state, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        due = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return due if due.tzinfo is not None else due.replace(tzinfo=UTC)


def auto_retry_attempt(checkpoint: dict[str, Any] | None) -> int:
    state = (checkpoint or {}).get("auto_retry")
    if not isinstance(state, dict):
        return 0
    try:
        return max(0, int(state.get("attempt", 0) or 0))
    except (TypeError, ValueError):
        return 0


#: 本地库正忙的原始报错带着整条 SQL 和参数，留这么多就够排查
DB_BUSY_DETAIL_CHARS = 300


def describe_failure(exc: BaseException, *, source: str) -> dict[str, Any]:
    """异常 → ``last_error`` 记录。``message`` 永远是一句短话；原始报错只进 ``detail``。"""
    found = classify_failure(exc)
    detail = error_text(exc)
    if found.code == "DB_BUSY":
        detail = detail.split("[SQL:", 1)[0].strip()[:DB_BUSY_DETAIL_CHARS]
    return error_record(
        found.code,
        transient=found.transient,
        source=source,
        detail=detail,
        message=found.user_message,
    )


__all__ = [
    "AUTO_RETRY_DELAYS_MINUTES",
    "MESSAGES",
    "FailureClass",
    "PreconditionError",
    "auto_retry_attempt",
    "auto_retry_due_at",
    "classify_failure",
    "clear_auto_retry",
    "schedule_auto_retry",
    "describe_failure",
    "error_record",
]
