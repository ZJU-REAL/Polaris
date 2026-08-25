"""错误签名归一化——零进展检测的共享基础（#354 引入，#360 提为共享模块）。

同签名 = 同一个错误在原地打转（零进展）；不同签名 = 修复改变了故障面（算进展）。
实验修复循环与引擎重规划计数共用这一套判定。
"""

from __future__ import annotations

import re

from app.services.managed_commands import failure_from_exception


def error_text(e: BaseException) -> str:
    """异常 → 人可读文本，空 str() 兜底。

    httpx.ReadTimeout 等异常的 str() 是空串，直接拼 f"{type}: {e}" 会产出
    「ReadTimeout: 」——用户被提问"怎么处理？"却拿到零信息（线上实测，用户只能
    回"详细原因"）。空时依次回退：cause 链 → 异常全名标注。"""
    report = failure_from_exception(e)
    name = type(e).__name__
    evidence: list[str] = []
    if report.command_display and report.command_display != "unknown command":
        evidence.append(f"command: {report.command_display}")
    if report.exit_status is not None:
        evidence.append(f"exit_status: {report.exit_status}")
    if report.stdout_tail:
        evidence.append(f"stdout:\n{report.stdout_tail}")
    if report.stderr_tail:
        evidence.append(f"stderr:\n{report.stderr_tail}")
    detail = str(e).strip()
    if not detail and e.__cause__ is not None:
        cause = e.__cause__
        cause_str = str(cause).strip()
        if cause_str:
            detail = f"{type(cause).__name__}: {cause_str}"
        elif type(cause).__name__ != name:
            # 同名空 cause 不重复自己（曾产出「ReadTimeout: ReadTimeout」）
            detail = type(cause).__name__
    if detail:
        text = f"{name}: {detail}"
    elif report.message and report.message != name:
        text = f"{name}: {report.message}"
    else:
        text = f"{name}（{type(e).__module__}.{name}，异常未附带详细信息）"
    return "\n".join([text, *evidence])


def error_signature(err_text: str) -> str:
    """报错文本 → 规范化签名（数字/十六进制/路径抹平，取 traceback 尾部关键行）。"""
    lines = [ln.strip() for ln in (err_text or "").strip().splitlines() if ln.strip()]
    # 取最像「结论」的尾部行：异常类型行优先，否则最后两行
    tail = [ln for ln in lines[-6:] if re.match(r"^[A-Za-z_.]+(Error|Exception|error)\b", ln)]
    picked = tail[-1:] if tail else lines[-2:]
    sig = " | ".join(picked)[:300]
    sig = re.sub(r"0x[0-9a-fA-F]+", "0xX", sig)
    sig = re.sub(r"\d+", "N", sig)
    sig = re.sub(r"/[^\s'\"]+", "/PATH", sig)
    return sig
