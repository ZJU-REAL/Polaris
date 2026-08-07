"""错误签名归一化——零进展检测的共享基础（#354 引入，#360 提为共享模块）。

同签名 = 同一个错误在原地打转（零进展）；不同签名 = 修复改变了故障面（算进展）。
实验修复循环与引擎重规划计数共用这一套判定。
"""

from __future__ import annotations

import re


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
