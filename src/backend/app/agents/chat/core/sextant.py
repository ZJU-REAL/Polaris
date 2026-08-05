"""Sextant（六分仪 · verify）：这轮回答站得住吗。

**确定性检查优先**，与 Voyage 的 Sextant 同一条原则：能靠数数和对照得出的结论，
不花模型的钱、不等模型的延迟。所以这里一次 LLM 都不调。

它检查的不是「答得好不好」——那不是机器该下的判断——而是三件能明确证伪的事：

1. 回答用了编号引用，这轮却一篇论文都没查到；
2. 编号超出了实际查到的篇数（[5] 但只查到 3 篇）——这是模型编号的经典跑偏；
3. 回答声称「查到了/库里有」，而这轮没有任何一次成功的检索。

命中任何一条只是**提请注意**，不改写回答、不拦截：判据是启发式的，误报要能被用户
一眼看穿并忽略。装成权威结论反而更糟。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 正文里的编号引用，如 [1]、[12]。中文方括号也认——模型经常混用。
_CITATION_RE = re.compile(r"[\[［](\d{1,2})[\]］]")

#: 「我查过了」这类断言。命中这些词却没有一次成功检索，就是在凭记忆答库内问题。
_RETRIEVAL_CLAIMS = ("我查到", "检索到", "库里有", "库中有", "根据检索", "搜索结果显示")


@dataclass(slots=True, frozen=True)
class Verdict:
    passed: bool
    notes: tuple[str, ...] = field(default_factory=tuple)


class Sextant:
    def verify(
        self, *, answer: str, sources: int, successful_tool_calls: int
    ) -> Verdict:
        """``sources`` = 这轮工具真的返回过的论文数；``successful_tool_calls`` = 成功的调用数。"""
        notes: list[str] = []
        cited = {int(m) for m in _CITATION_RE.findall(answer or "")}

        if cited and sources == 0:
            notes.append("回答里有编号引用，但这一轮没有查到任何论文")
        elif cited and max(cited) > sources:
            notes.append(f"引用编号最大到 [{max(cited)}]，但这一轮只查到 {sources} 篇论文")

        claimed = any(claim in (answer or "") for claim in _RETRIEVAL_CLAIMS)
        if successful_tool_calls == 0 and claimed:
            notes.append("回答说「查到了」，但这一轮没有任何一次成功的检索")

        return Verdict(passed=not notes, notes=tuple(notes))
