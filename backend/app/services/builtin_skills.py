"""内置技能种子（docs/skill-system.md §2）。

scope=builtin 只读，slug 全局唯一；用户可在技能库「复制为我的技能」后修改。
启动时由 skills.ensure_builtin_skills() 按 slug 幂等插入（已存在则跳过，
不覆盖——避免踩掉管理员手工修正；内容大改时应换新 slug 或出数据迁移）。
"""

from typing import Any

BUILTIN_SKILLS: list[dict[str, Any]] = [
    {
        "slug": "relevance-rubric-cs-ai",
        "kind": "rubric",
        "name": "文献相关性评估标准",
        "name_en": "Literature Relevance Rubric",
        "description": "补充判断一篇论文与研究方向相关性的打分细则（0-1）",
        "targets": ["wiki.score_relevance"],
        "body": """\
打分时按以下细则校准（在研究方向定义的 rubric 之外补充）：
- 0.9-1.0：论文直接研究同一问题，或提出的方法可直接用于本方向的核心实验。
- 0.7-0.9：同一子领域，方法/结论对本方向有明确借鉴意义（如可作 baseline、提供关键技巧）。
- 0.5-0.7：相邻领域但共享关键技术（如同用某类架构/训练策略），需要迁移才能利用。
- 0.3-0.5：仅背景相关：帮助理解领域现状，但不影响方法设计。
- <0.3：主题重叠停留在关键词层面。
注意事项：
- 综述（survey）按其覆盖面单独衡量：覆盖本方向核心问题的综述给 0.6-0.8。
- 只看标题摘要难以判断时，宁可给中间分（0.5 左右）并在 reason 里说明不确定因素，
  不要因为标题花哨而给高分。
- benchmark/数据集论文若与本方向评测相关，至少 0.6。""",
    },
    {
        "slug": "librarian-note-style",
        "kind": "guidance",
        "name": "论文精读笔记规范",
        "name_en": "Paper Reading-note Style",
        "description": "wiki 页编译时的内容取舍与行文规范",
        "targets": ["wiki.compile"],
        "body": """\
编写论文 wiki 页时遵循：
- TL;DR 用一句话回答「这篇论文解决了什么问题、方法核心一句话是什么、效果如何」。
- 方法部分写「机制」而不是「流程复述」：解释为什么这样设计有效，标出与常规做法的差异点。
- 「可借鉴点」必须具体到可操作：写「其 curriculum 调度公式(式3)可直接用于我们的数据混合比例」，
  不写「其训练策略值得借鉴」。
- 实验部分只保留支撑核心 claim 的数字：主表主指标 + 最有信息量的消融，其余略去。
- 明确写出局限性与作者未讨论的疑点（如 baseline 选择偏弱、只在小规模验证）。
- 术语首次出现给中文+英文原词，此后统一用英文原词。""",
    },
    {
        "slug": "gap-analysis-lenses",
        "kind": "guidance",
        "name": "Research Gap 分析视角",
        "name_en": "Research Gap Analysis Lenses",
        "description": "gap 分析时系统性检查的六个视角",
        "targets": ["forge.gap_analysis"],
        "body": """\
逐一检查以下视角，每个视角至少考虑一次再决定是否产出 gap：
1. 假设盲区：领域内公认假设中，哪些从未被系统验证？
2. 组合空白：两条独立发展的技术线是否从未被组合评估？
3. 规模外推：只在小模型/小数据上验证过的结论，规模变化后是否仍成立？
4. 评测缺口：现有 benchmark 测不到但实际部署关键的能力是什么？
5. 失败案例：多篇论文都提到但都绕开的失败模式是什么？
6. 反向问题：领域都在优化 X，但「牺牲 X 换 Y」的权衡是否有人研究？
产出的每个 gap 必须引用知识库中的具体论文作为证据（谁做到了哪一步、缺口在哪），
不允许凭空断言「没有人研究过」。""",
    },
    {
        "slug": "idea-scoring-rubric",
        "kind": "rubric",
        "name": "Idea 四维打分细则",
        "name_en": "Idea 4-dim Scoring Rubric",
        "description": "新颖性/可行性/可操作性/影响力的锚点分数定义",
        "targets": ["forge.score"],
        "body": """\
四个维度按锚点打分（0-10），先对锚点再微调：
- 新颖性：3=已有工作的直接增量；5=已知技术的新组合或新场景；7=对公认假设提出有证据的质疑；
  9=开辟新问题或新范式。与知识库论文重叠越多分越低，必须点名最接近的论文。
- 可行性：以「实验室 1-2 人、数张 GPU、3 个月」为基准。9=现有开源代码改动即可验证；
  7=需要自研训练但规模可控；5=依赖大规模预训练或稀缺数据；3=依赖不可得资源。
- 可操作性：9=能直接写出第一个实验的脚本级描述（数据、模型、指标全明确）；
  7=主实验清晰但评测方案需设计；5=思路清楚但切入实验模糊；3=只有愿景。
- 影响力：9=若成立将改变领域常规做法；7=子领域内会被 follow；5=一篇扎实论文；3=增量结果。
打分独立进行，禁止四维互相拉扯（如因新颖性高就抬可行性）。理由必须引用 idea 文本或
知识库论文的具体内容。""",
    },
    {
        "slug": "idea-generation-quality",
        "kind": "guidance",
        "name": "高质量 Idea 生成准则",
        "name_en": "Idea Generation Quality Bar",
        "description": "生成候选 idea 时的质量门槛与反模式",
        "targets": ["forge.generate"],
        "body": """\
每个候选 idea 必须包含：动机（哪个 gap，引用哪些论文）、核心假设（一句可证伪的陈述）、
方法概述（与最近邻工作的差异点）、最小验证实验（数据/模型/指标/预期现象）、主要风险。
避免以下反模式：
- 「X + Y」式无动机缝合：组合必须有机制层面的理由。
- 万金油描述：把「用 LLM 改进…」「引入注意力…」当创新点。
- 不可证伪：预期结果写成「可能有帮助」而没有可测量的判据。
- 隐形巨型工程：表面小改动实际需要重训基础模型。
宁可少生成，不要凑数。""",
    },
    {
        "slug": "conference-review-rubric",
        "kind": "rubric",
        "name": "顶会论文评审标准",
        "name_en": "Top-venue Review Rubric",
        "description": "按 NeurIPS/ICLR 风格维度评审论文",
        "targets": ["review.referees"],
        "body": """\
按顶会标准逐维评估，每条意见注明章节/行号级出处：
- Soundness：核心 claim 是否都有实验或证明支撑？统计显著性、随机种子、
  公平对比（同算力/同数据）是否交代？
- Novelty：与最相关的 3 篇工作逐一对比，指出真正的增量是什么；
  缺失关键引用直接指出。
- Clarity：方法是否可复现（超参、数据处理、伪代码）？符号是否一致？
- Significance：结果对领域的意义；改进幅度是否超出噪声范围。
评审语气：具体、可执行、不人身化。每条 weakness 附「作者可以怎样改进」。
区分「致命缺陷」（结论不成立）与「可修复问题」（补实验/改写作可解决），
最终意见里明确列出哪些问题属于哪类。""",
    },
    {
        "slug": "academic-writing-style",
        "kind": "guidance",
        "name": "学术写作规范",
        "name_en": "Academic Writing Style",
        "description": "论文分节撰写的通用行文规范",
        "targets": ["writing.section"],
        "body": """\
行文遵循：
- 每段一个论点，段首句即论点句；段内其余句子只做支撑，不引入新论点。
- claim 与证据对齐：每个定量表述必须紧跟出处（实验编号/表号/引用），
  禁止「significantly」「substantially」等词不带数字出现。
- 时态：方法描述用一般现在时，实验执行用过去时。
- 主动语态优先；「We propose/show/find」开头的句子占比不超过段落的一半，避免句式单调。
- 首次出现的缩写给全称；符号在 Method 首次定义后全文复用，不换记号。
- 不留占位：写不出的内容明确标注 TODO 并说明缺什么数据，不编造。""",
    },
    {
        "slug": "abstract-writing",
        "kind": "guidance",
        "name": "Abstract 写作技能",
        "name_en": "Abstract Writing",
        "description": "五句式摘要结构",
        "targets": ["writing.section(abstract)"],
        "body": """\
摘要用五句式骨架（可微调但不偏离）：
1. 背景与问题：领域关心什么，现有方法卡在哪（一句，不堆引用）。
2. 缺口：为什么现有方案不够（点出机制层面的原因）。
3. 方法：我们提出什么，核心机制一句话。
4. 结果：最重要的 1-2 个数字，写明基线与提升幅度、数据集。
5. 意义：这个结果说明什么/解锁什么。
禁止：悬念式写法（「结果令人惊讶」）、罗列贡献编号、超过 200 词、
出现正文未支撑的数字。""",
    },
    {
        "slug": "related-work-writing",
        "kind": "guidance",
        "name": "Related Work 综述技能",
        "name_en": "Related Work Writing",
        "description": "按主题线组织相关工作并落到差异点",
        "targets": ["writing.related_work"],
        "body": """\
组织方式：
- 按 2-4 条主题线分段，每段 = 一条研究线的演进 + 与本文的关系；禁止逐篇流水账。
- 每段结尾一句「与本文的差异」：他们做到 X，本文在 Y 上不同/更进一步。
- 同一段内引用按逻辑顺序（问题提出 → 方法演进 → 目前最好），不是按年份罗列。
- 对最接近的 1-2 篇竞品工作单独展开对比，明确本文不是其简单扩展。
- 只引用候选集内确认存在的文献；没有合适引用支撑的论述直接删除。""",
    },
    {
        "slug": "debate-personas-classic",
        "kind": "persona",
        "name": "经典辩论三人设",
        "name_en": "Classic Debate Personas",
        "description": "Idea 辩论的正方/反方/裁判人设包（顺序即分工）",
        "targets": ["review.debate"],
        "personas": [
            {"name": "建设性支持者", "stance": "挖掘想法的最大潜力，为其找到最强的成立条件与证据"},
            {
                "name": "复现怀疑派",
                "stance": "专挑不可复现与资源不切实际之处，要求给出可验证的最小实验",
            },
            {"name": "中立方法论裁判", "stance": "只依据双方论据的证据强度判胜负，惩罚空泛断言"},
        ],
        "body": "人设按顺序分工：第 1 位为正方、第 2 位为反方、第 3 位为裁判。"
        "启用后替代内置默认人设；启动辩论时显式传入的人设优先于本技能。",
    },
    {
        "slug": "referee-personas-strict",
        "kind": "persona",
        "name": "严格评审三人设",
        "name_en": "Strict Referee Personas",
        "description": "论文评审的三位评审员人设包（方法/实验/写作各一）",
        "targets": ["review.referees"],
        "personas": [
            {"name": "方法审稿人", "stance": "紧盯方法正确性与理论依据，检查每个 claim 的支撑"},
            {"name": "实验审稿人", "stance": "检查基线公平性、消融完整性与统计显著性"},
            {"name": "写作审稿人", "stance": "检查结构清晰度、符号一致性与可复现细节"},
        ],
        "body": "三位评审员各司其职，避免意见同质化；启用后替代内置默认评审员人设。",
    },
    {
        "slug": "experiment-design-checklist",
        "kind": "guidance",
        "name": "实验设计规范",
        "name_en": "Experiment Design Checklist",
        "description": "制定实验计划时的方法学检查单",
        "targets": ["experiment.plan"],
        "body": """\
实验计划必须满足：
- 假设先行：写出本实验要证伪/证实的假设，主指标直接对应该假设。
- 基线充分：至少一个「trivial baseline」（如随机/多数类/不加改动的原方法）+
  一个当前最强可复现方法；基线与本方法同数据、同算力预算、同调参力度。
- 消融闭环：方法每个组件对应一条消融；预期哪条消融掉点最多要事先写出。
- 随机性控制：≥3 个种子或说明为何不需要；报告均值±标准差。
- 规模阶梯：先最小可运行规模冒烟，再逐级放大；每级设继续/中止判据。
- 失败预案：主假设不成立时，计划里写明可以回答的次级问题是什么。""",
    },
    {
        "slug": "lit-review-sketch",
        "kind": "workflow",
        "name": "文献综述速写",
        "name_en": "Literature Review Sketch",
        "description": "三步产出一篇主题综述草稿（脉络 → 对比 → 成文）",
        "targets": ["navigator.free_plan"],
        "steps": [
            {
                "title": "梳理主题脉络",
                "action": "llm.complete",
                "params": {
                    "stage": "librarian",
                    "prompt": "围绕主题「{goal}」梳理研究脉络：主要研究线、代表性方法、"
                    "演进关系与当前共识/分歧，输出结构化提纲。",
                },
                "acceptance": "输出包含研究线划分与演进关系的提纲",
                "requires_gate": None,
            },
            {
                "title": "方法对比分析",
                "action": "llm.complete",
                "params": {
                    "stage": "librarian",
                    "prompt": "基于主题「{goal}」，对主要方法做对比分析：核心机制差异、"
                    "适用场景、已知局限，用表格+短评呈现。",
                },
                "acceptance": "输出包含方法对比与局限分析",
                "requires_gate": None,
            },
            {
                "title": "成文综述草稿",
                "action": "llm.complete",
                "params": {
                    "stage": "writing",
                    "prompt": "综合前述脉络与对比，为主题「{goal}」写一篇中文综述草稿"
                    "（引言/主题分线综述/开放问题），按主题线组织，不逐篇罗列。",
                },
                "acceptance": "输出为按主题线组织的综述草稿",
                "requires_gate": None,
            },
        ],
        "body": "适用于快速形成某主题的综述初稿；在「AI 任务」中输入主题即可运行。",
    },
    {
        "slug": "rebuttal-draft",
        "kind": "workflow",
        "name": "Rebuttal 起草",
        "name_en": "Rebuttal Draft",
        "description": "把审稿意见整理成逐条回应草稿",
        "targets": ["navigator.free_plan"],
        "steps": [
            {
                "title": "提炼审稿意见要点",
                "action": "llm.complete",
                "params": {
                    "stage": "review",
                    "prompt": "以下是审稿意见与背景：{goal}\n请把意见拆解为逐条编号的"
                    "关切点，标注类型（事实性错误/缺实验/写作问题/误解），并判断哪些必须回应。",
                },
                "acceptance": "输出为逐条编号且带类型标注的意见清单",
                "requires_gate": None,
            },
            {
                "title": "逐条起草回应",
                "action": "llm.complete",
                "params": {
                    "stage": "writing",
                    "prompt": "针对上一步的意见清单（背景：{goal}），逐条起草礼貌而有依据的"
                    "回应：先感谢并复述关切，再给出回应策略（澄清/补实验计划/修改承诺），"
                    "误解处引用原文位置澄清。",
                },
                "acceptance": "每条意见都有对应回应段落",
                "requires_gate": None,
            },
        ],
        "body": "适用于收到审稿意见后快速形成 rebuttal 骨架；运行时把审稿意见粘贴进任务目标。",
    },
    {
        "slug": "reproducible-code-style",
        "kind": "guidance",
        "name": "实验代码可复现性规范",
        "name_en": "Reproducible Experiment Code",
        "description": "生成实验代码时的工程规范",
        "targets": ["experiment.setup"],
        "body": """\
生成实验代码遵循：
- 所有超参集中在一个 config（文件顶部 dataclass 或 yaml），不散落在函数默认值里。
- 固定随机种子（python/numpy/torch/cuda）且种子本身是配置项。
- 每次运行把完整 config、git 状态（如有）、环境信息 dump 到输出目录。
- 指标写成结构化 jsonl（step, metric, value），不要只打印到 stdout。
- 数据处理与训练解耦：预处理产物落盘并带版本号，训练脚本只读产物。
- 脚本必须支持 --smoke 模式：小数据/少步数跑通全流程后立即退出。
- 失败要响亮：不吞异常，断言输入形状与数值范围。""",
    },
]
