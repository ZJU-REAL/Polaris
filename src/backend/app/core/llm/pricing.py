"""模型标价与费用估算（美元）。

界面上显示的费用只有一个来源：``llm_usage`` 里逐次调用记下的四个数（没命中缓存的
输入、读缓存、写缓存、输出）乘以这里的标价。读缓存按输入价的 0.1 倍、写缓存按
1.25 倍（5 分钟档）计——提示缓存开着的时候，按「输入全价」算会把费用高估好几倍。

这是**估算**：网关可能有自己的加价或折扣，1 小时档的缓存写入是 2 倍而这里统一按
1.25 倍，没登记标价的模型按 0 计（向量与重排通常是本地或免费的端点；要计价就在
``PRICES`` 里登记）。所以凡是把它给人看的地方都要标「约」。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 读缓存 / 写缓存相对输入价的倍数（Anthropic 标准计价，5 分钟档）。
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """美元 / 百万 token。"""

    input: float
    output: float


#: 官方模型名 → 标价。网关风格的 ``anthropic/`` 前缀在查表前去掉。
PRICES: dict[str, ModelPrice] = {
    "claude-fable-5-2": ModelPrice(8.0, 40.0),
    "claude-fable-5-1": ModelPrice(10.0, 50.0),
    "claude-opus-5-5": ModelPrice(4.0, 20.0),
    "claude-opus-5": ModelPrice(5.0, 25.0),
    "claude-opus-4-8": ModelPrice(5.0, 25.0),
    "claude-sonnet-5-5": ModelPrice(2.0, 10.0),
    "claude-sonnet-5": ModelPrice(2.0, 10.0),
    "claude-haiku-5-5": ModelPrice(0.1, 0.5),
    "claude-haiku-4-5": ModelPrice(1.0, 5.0),
}

#: 不是对话模型的环节：没登记标价时按 0 计，不借对话模型的价。
FREE_STAGES = frozenset({"embedding", "rerank"})

#: 模型名认不出来的对话调用按它计（平台只接这一个模型）。
DEFAULT_CHAT_PRICE = PRICES["claude-opus-5"]


def price_for(model: str, stage: str = "") -> ModelPrice | None:
    """这次调用按什么价计；None = 不计价（向量 / 重排且没登记标价）。"""
    known = PRICES.get((model or "").rsplit("/", 1)[-1])
    if known is not None:
        return known
    if stage in FREE_STAGES:
        return None
    return DEFAULT_CHAT_PRICE


def cost_usd(
    *,
    model: str,
    stage: str = "",
    prompt_tokens: int,
    completion_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """一次（或一组同模型同环节）调用的估算费用。

    ``prompt_tokens`` 是送进去的全部输入（含读缓存与写缓存的部分），与 ``llm_usage``
    的口径一致；读 / 写缓存的部分从里面拆出来按各自的倍数计。
    """
    price = price_for(model, stage)
    if price is None:
        return 0.0
    cache_read = max(0, cache_read_tokens)
    cache_write = max(0, cache_write_tokens)
    uncached = max(0, prompt_tokens - cache_read - cache_write)
    input_cost = (
        uncached + cache_read * CACHE_READ_MULTIPLIER + cache_write * CACHE_WRITE_MULTIPLIER
    ) * price.input
    return (input_cost + max(0, completion_tokens) * price.output) / 1e6
