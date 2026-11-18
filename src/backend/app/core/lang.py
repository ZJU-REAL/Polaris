"""界面语言：服务端生成的、直接给人看的短句用哪种语言写。

前端是单语界面加中英切换，绝大多数文案在前端。少数句子只能由后端拼——PolarisBuddy
的问候语（数字来自 SQL）、工具调用的一行摘要（内容来自调用结果）——它们得跟着界面
语言走，否则英文界面里会冒出一行中文。

约定与已有的 ``lang`` 查询参数一致（``/voyages/{id}/ai-disclosure``）：值是 ``zh`` 或
``en``。没显式给时看 ``Accept-Language``，再没有就是中文（历史默认，老客户端不变）。
"""

from __future__ import annotations

import contextvars
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Literal

Lang = Literal["zh", "en"]

DEFAULT_LANG: Lang = "zh"
LANGS: tuple[Lang, ...] = ("zh", "en")

_current: contextvars.ContextVar[Lang] = contextvars.ContextVar("polaris_lang", default="zh")


def _from_accept_language(header: str | None) -> Lang | None:
    """``Accept-Language`` 里排最前的那个我们认识的语言；一个都不认识返回 None。"""
    ranked: list[tuple[float, int, Lang]] = []
    for pos, part in enumerate((header or "").split(",")):
        tag, _, params = part.strip().partition(";")
        primary = tag.strip().lower().split("-")[0]
        if primary not in LANGS:
            continue
        quality = 1.0
        params = params.strip()
        if params.startswith("q="):
            try:
                quality = float(params[2:])
            except ValueError:
                quality = 0.0
        if quality > 0:
            ranked.append((-quality, pos, primary))  # type: ignore[arg-type]
    return min(ranked)[2] if ranked else None


def resolve_lang(explicit: str | None = None, accept_language: str | None = None) -> Lang:
    """显式参数 > ``Accept-Language`` > 中文。认不出的显式值当作没给，不报错。"""
    value = (explicit or "").strip().lower().split("-")[0]
    if value in LANGS:
        return value  # type: ignore[return-value]
    return _from_accept_language(accept_language) or DEFAULT_LANG


def pick(lang: str | None, zh: str, en: str) -> str:
    return en if lang == "en" else zh


def current_lang() -> Lang:
    return _current.get()


def tr(zh: str, en: str) -> str:
    """按当前语言二选一。当前语言由 :func:`use_lang` 设定，缺省中文。"""
    return en if _current.get() == "en" else zh


@contextmanager
def use_lang(lang: str | None) -> Iterator[None]:
    """在这段代码里让 :func:`tr` 按 ``lang`` 取值。"""
    token = _current.set("en" if lang == "en" else "zh")
    try:
        yield
    finally:
        _current.reset(token)


async def iterate_in_lang[T](events: AsyncIterator[T], lang: str | None) -> AsyncIterator[T]:
    """逐个取事件，每取一个都在 ``lang`` 下进行。

    流式响应的生成器跨着很多次让出执行；把整个循环包在一个 :func:`use_lang` 里，
    收尾可能落在另一个上下文里（重置变量会报错）。所以只在「取下一个事件」这一步
    里设语言——工具调用和它的一行摘要正是在这一步里生成的。
    """
    iterator = events.__aiter__()
    while True:
        with use_lang(lang):
            try:
                event = await iterator.__anext__()
            except StopAsyncIteration:
                return
        yield event
