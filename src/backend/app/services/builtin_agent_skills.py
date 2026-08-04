"""助手的内置技能种子。

写这些种子时只有一条纪律：**description 是触发条件，不是功能介绍**。它是目录里常驻
system prompt 的唯一内容，模型全凭它判断这次要不要 skill_load。写成"这是什么"而不是
"什么时候用"，技能就永远不会被加载——渐进披露的成败全在这一句上。

数量刻意少：目录每轮都要重发，五个写得好的胜过十四个凑数的。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.agent_skills import upsert_from_md

#: (SKILL.md 文本, 附件)。附件是给模型抄的模板，不是可执行的东西。
BUILTIN_AGENT_SKILLS: list[tuple[str, dict[str, str]]] = [
    (
        """\
---
name: polaris-search-playbook
description: >
  在平台里检索文献时使用：分不清该用 scan_papers、grep_fulltext、search_papers
  还是 search_chunks，或一次检索返回空、需要换打法再试时，先加载本技能。
invocation: auto
---

# 检索工具选型

四个检索工具各管一段，选错了既费钱又查不到：

- **scan_papers**：不知道语料里有什么时先用它铺开。每篇只回标题和年份，一次可以看
  50 篇。它是地图，不是答案。
- **grep_fulltext**：找确切的字面串（模型名、数据集名、公式符号、特定短语）。语义
  检索对这类查询反而不准。
- **search_papers**：按意思找"哪几篇讲了 X"。回 TL;DR，适合从题意收敛到 3~5 篇。
- **search_chunks**：已经知道要什么、想拿能引用的原文段落时用。粒度最细也最贵。

节奏：宽（scan / grep）→ 收敛（search_papers）→ 深读（read_fulltext / read_wiki）。
跳过宽扫直接深读，容易在错的论文上花光轮次。

空结果的处理：换同义词再试一次（中英文都试）；关键词模式与语义模式互换再试一次；
两轮都空就如实说没查到，并报告试过的查法。不要用第三种说法反复试到轮次耗尽。
""",
        {},
    ),
    (
        """\
---
name: literature-triage
description: >
  用户要求批量判断一批论文与方向的相关性时使用：例如"帮我筛一下这批论文"、
  "这些跟我的方向相关吗"、"每日推送里哪些值得看"。
allowed-tools: [scan_papers, search_papers, get_paper, read_wiki]
invocation: auto
---

# 文献初筛

逐篇给出「保留 / 存疑 / 剔除」三档结论，而不是每篇写一段读后感。

流程：

1. 先确认方向说明（research statement）。用户没给就先问，不要拿论文标题反推方向。
2. 对每篇：标题与 TL;DR 能定档的直接定档；定不了的才 read_wiki 看解读。
   **不要逐篇读全文**——初筛的价值就在快。
3. 输出一张表：标题、档位、一句话理由。存疑的单独说明缺什么信息才能定档。

判据优先级：研究对象是否吻合 > 方法是否在方向关心的类型里 > 仅仅共享关键词。
只有关键词重叠的一律存疑，不要保留。
""",
        {},
    ),
    (
        """\
---
name: paper-deep-read
description: >
  用户要求认真读一篇论文并给出结构化解读时使用：例如"帮我精读这篇"、
  "这篇的方法到底是什么"、"这篇和我们的做法有什么区别"。
allowed-tools: [get_paper, read_fulltext, read_wiki, get_concept, grep_fulltext]
invocation: auto
---

# 单篇精读

按 `template.md` 的结构组织产出。要点：

- 先 read_wiki 看有没有现成解读；有就以它为骨架，用 read_fulltext 补关键细节，
  不要从零重读。
- 方法部分必须给出**机制**（怎么做到的），不是复述贡献列表（做到了什么）。
- 实验部分挑主结果表：基线是谁、提升多少、哪个消融最说明问题。
- 与用户方向的关系单独成段，没有把握就明说"从摘要看不出关联"。
- 引用原文时用 grep_fulltext 定位原句，不要凭记忆转述数字。
""",
        {
            "template.md": """\
# {论文标题}

## 一句话
（它解决什么问题、用什么办法、效果如何）

## 方法机制
（关键设计怎么工作，配公式或流程；不是贡献列表）

## 主要证据
（主结果表：基线、提升幅度、最有说服力的消融）

## 局限与开放问题

## 与你方向的关系
（对得上就说对在哪；对不上就明说）
""",
        },
    ),
    (
        """\
---
name: citation-hygiene
description: >
  回答里需要引用论文、给出数字或结论出处时使用；用户质疑"这个说法哪来的"
  或要求核对引用时也加载。
invocation: auto
---

# 引用纪律

- 每个论文相关的事实陈述都要能落到具体论文：句末标 [n]，n 是本轮工具结果里的论文。
- **数字必须来自工具结果**。检索结果里没有的数字不要写——宁可说"原文未给出具体数值"。
- 分清三种话并让读者也分得清：论文说的、多篇论文综合出的、你自己的判断。
- 工具没查到某篇论文时，不要凭训练记忆补它的内容；明说"库里没有这篇"。
- 用户质疑出处时：用 grep_fulltext 找到原句贴出来，找不到就撤回该陈述。
""",
        {},
    ),
    (
        """\
---
name: research-landscape
description: >
  用户要求梳理一个方向的全貌时使用：例如"这个方向的相关工作有哪些"、
  "帮我做个小综述"、"最近这个领域有什么进展"。这类任务应优先派子智能体。
allowed-tools: [run_subagent, scan_papers, search_papers, get_concept, list_concepts]
invocation: auto
---

# 方向梳理

这类任务会产生几十条中间检索结果，**直接派 run_subagent**，不要自己逐篇扫——
中间结果会占满我们的对话上下文，后面几轮全在重发它们。

给子智能体的任务描述要自足（它看不到本对话），必须包含：方向的完整描述、期望的
产出结构（按主题分组，每组给代表论文的标题与 id）、以及范围限制（只看库内还是
含库外）。

拿到结论后：按主题组织答案，每个主题给 2~3 篇代表作并标 [n]；说明这是基于库内
语料的梳理，库外可能另有工作。
""",
        {},
    ),
]


async def ensure_builtin_agent_skills(session: AsyncSession) -> int:
    """幂等种入内置技能（按 slug；已存在则更新正文——种子是代码的一部分，跟着代码走）。

    与旧系统"已存在不覆盖"不同：这些种子没有用户改动可保护（内置技能只读，想改就
    fork），落后的种子只会误导模型。
    """
    count = 0
    for md, files in BUILTIN_AGENT_SKILLS:
        await upsert_from_md(session, text=md, user_id=None, scope="builtin", files=files)
        count += 1
    await session.commit()
    return count
