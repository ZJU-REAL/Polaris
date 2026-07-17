"""确定性假 Provider：无需网络与 API key，测试与无 key 演示用。

对 prompt 做简单模板回显；识别到 JSON 请求（Navigator 规划 / Sextant 判定 /
相关性打分 / 概念定义 / Librarian 编译的 system prompt 标记）时返回符合
对应 schema 的合法 JSON / markdown。嵌入用 token-hash 词袋的确定性向量。
"""

import hashlib
import json
import math
import re
from collections.abc import AsyncIterator, Sequence

from app.core.llm.base import CompletionResult, LLMProvider, Message, RerankResult

# 与 navigator.py / sextant.py / actions_wiki.py / actions_ideas.py /
# services/projects.py 的 prompt 对齐的识别标记
_PLAN_MARKER = '"steps"'
_PLAN_EDIT_MARKER = "POLARIS_PLAN_EDIT"  # 计划编辑（loop 失败回灌，navigator.on_result）
_VERDICT_MARKER = '"passed"'
_RELEVANCE_MARKER = '"score"'
_CONCEPTS_MARKER = "概念列表："
_LIBRARIAN_MARKER = "TL;DR"
_INTERVIEW_MARKER = '"out_of_scope"'
_GAPS_MARKER = '"gaps"'  # forge gap 分析
_IDEAS_MARKER = '"ideas"'  # forge 候选生成
_IDEA_SCORE_MARKER = '"operability"'  # forge 四维打分
_JUDGE_MARKER = '"winner"'  # debate 裁判判定
_EXP_PLAN_MARKER = '"repro_strategy"'  # experiment 计划
_EXP_CODE_MARKER = '"requirements.txt"'  # experiment 代码生成/修复/迭代改进
_EXP_REPORT_MARKER = "## 实验报告"  # experiment 报告
_EXP_REFLECTION_MARKER = '"hypothesis_updates"'  # experiment 迭代 reflection（M5-A）
_EXP_PLOT_MARKER = '"plot_figures.py"'  # experiment 绘图脚本（M5-A）
_EXP_FIGQC_MARKER = "图表质检员"  # experiment 图表 VLM 质检（M5-A，多模态）
_READING_MARKER = "论文阅读助手"  # AI 伴读（papers.py CHAT_SYSTEM_PROMPT_TEMPLATE）
_LIBRARY_MARKER = "文献库研究助手"  # 文献库对话（library_chat.py）
_WRITE_SECTION_MARKER = "POLARIS_WRITING_SECTION"  # 论文分节撰写（M5-B）
_WRITE_RELATED_MARKER = "POLARIS_RELATED_WORK"  # Related Work 候选集内选引（M5-B）
_WRITE_REFLECT_MARKER = "POLARIS_WRITING_REFLECT"  # 写作 self-reflection（M5-B）
# M5-C 论文评审（actions_review.py 五个 system prompt 对齐）
_PAPER_REVIEWER_MARKER = "POLARIS_PAPER_REVIEWER"  # 评审员意见（多模态）
_REVIEW_GUARDRAIL_MARKER = "POLARIS_REVIEW_GUARDRAIL"  # 逐员 guardrail 校验
_REVIEW_SUPPORT_MARKER = "POLARIS_REVIEW_SUPPORT"  # 引用支撑性判定
_REVIEW_META_MARKER = "POLARIS_REVIEW_META"  # meta-review 总结
_REVIEW_FACTCHECK_MARKER = "POLARIS_REVIEW_FACTCHECK"  # claim 抽查
# Idea 2.0 深耕（actions_proposal.py 的 system prompt 对齐，docs/api-idea2.md）
_GOAL_EXPLORE_MARKER = "POLARIS_GOAL_EXPLORE"  # 目标构建工具循环
_GOAL_REFINE_MARKER = "POLARIS_GOAL_REFINE"  # 审批意见并入目标
_PROPOSAL_RELATED_MARKER = "POLARIS_PROPOSAL_RELATED"  # 相关工作（工具循环）
_PROPOSAL_DESIGN_MARKER = "POLARIS_PROPOSAL_DESIGN"  # 方案设计
_PROPOSAL_EXPERIMENTS_MARKER = "POLARIS_PROPOSAL_EXPERIMENTS"  # 实验计划 + smoke_plan
_NOVELTY_CHECK_MARKER = "POLARIS_NOVELTY_CHECK"  # 新颖性三档判定
_PROPOSAL_RISKS_MARKER = "POLARIS_PROPOSAL_RISKS"  # 风险与备选
_PROPOSAL_TITLE_MARKER = "POLARIS_PROPOSAL_TITLE"  # 标题/概述/预期成果
_PROPOSAL_REVIEW_MARKER = "POLARIS_PROPOSAL_REVIEW"  # 专职评审员
_PROPOSAL_REVISE_MARKER = "POLARIS_PROPOSAL_REVISE"  # 作者修订

EMBEDDING_DIM = 1024  # 与真实 embedding 模型（BGE-M3）维度一致

_FAKE_PLAN = {
    "steps": [
        {
            "title": "分析目标",
            "action": "llm.complete",
            "params": {"stage": "default", "prompt": "围绕目标给出分析要点：{goal}"},
            "acceptance": "输出包含对目标的分析要点",
        }
    ]
}


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（len/4，最少 1）。"""
    return max(1, len(text) // 4)


def fake_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """确定性词袋嵌入：token 哈希到 dim 维桶后 L2 归一化（关键词重叠 → 余弦相似）。"""
    vec = [0.0] * dim
    for token in re.findall(r"\w+", text.lower()):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        vec[int.from_bytes(digest[:4], "big") % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class FakeProvider(LLMProvider):
    name = "fake"

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
    ) -> CompletionResult:
        full_text = "\n".join(m.content for m in messages)
        if images and _PAPER_REVIEWER_MARKER in full_text:
            # 多模态论文评审员：确定性 JSON（人设不同分便于聚合测试）
            content = self._respond_paper_reviewer(full_text)
        elif images and _LIBRARIAN_MARKER in full_text:
            # 多模态图文编译：librarian markdown 里插入一行 ![[fig:0]] 图片标记
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            content = self._respond_librarian(last_user, with_figure=True)
        elif images and _EXP_FIGQC_MARKER in full_text:
            # 多模态实验图表质检：确定性通过并逐图配假图注
            content = json.dumps(
                {
                    "passed": True,
                    "figures": [
                        {"index": i, "caption": f"（fake）实验图注 {i}"} for i in range(len(images))
                    ],
                    "issues": [],
                },
                ensure_ascii=False,
            )
        elif images:
            # 多模态（论文图筛选注释）：确定性选前两张图并配假图注 + 类型
            kinds = ["method", "experiment"]
            content = json.dumps(
                [
                    {"index": i, "important": True, "kind": kinds[i], "caption": "（fake）图注"}
                    for i in range(min(2, len(images)))
                ],
                ensure_ascii=False,
            )
        elif _PLAN_EDIT_MARKER in full_text:
            # 计划编辑（loop 失败回灌）：确定性给出一个替代步骤
            content = self._respond_plan_edit()
        elif _EXP_REFLECTION_MARKER in full_text:
            # 迭代 reflection：按调用计数依次 improve → improve → stop（循环，确定性可测）
            content = self._respond_exp_reflection()
        elif _EXP_PLOT_MARKER in full_text:
            content = self._respond_exp_plot()
        else:
            content = self._respond(messages, model)
        prompt_len = sum(estimate_tokens(m.content) for m in messages)
        return CompletionResult(
            content=content,
            model=model,
            finish_reason="stop",
            usage={
                "prompt_tokens": prompt_len,
                "completion_tokens": estimate_tokens(content),
            },
        )

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        result = await self.complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens
        )
        chunk = 64
        for i in range(0, len(result.content), chunk):
            yield result.content[i : i + chunk]

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        return [fake_embedding(t) for t in texts]

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        model: str,
        top_n: int | None = None,
    ) -> RerankResult:
        """确定性重排：查询/文档词集 Jaccard 重叠打分，同分按原下标稳定排序。"""
        query_tokens = set(re.findall(r"\w+", query.lower()))

        def score_of(doc: str) -> float:
            doc_tokens = set(re.findall(r"\w+", doc.lower()))
            union = query_tokens | doc_tokens
            if not union:
                return 0.0
            return len(query_tokens & doc_tokens) / len(union)

        scored = [(i, score_of(doc)) for i, doc in enumerate(documents)]
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        if top_n is not None:
            scored = scored[:top_n]
        total = estimate_tokens(query) + sum(estimate_tokens(d) for d in documents)
        return RerankResult(results=scored, usage={"total_tokens": total})

    @staticmethod
    def _respond(messages: Sequence[Message], model: str) -> str:
        full_text = "\n".join(m.content for m in messages)
        last_user = next(
            (m.content for m in reversed(messages) if m.role == "user"),
            full_text,
        )
        # M5-C 论文评审五 marker：prompt 内嵌 LaTeX 源/评审意见（可能撞其他 marker），
        # 且 guardrail/支撑性输出与 sextant 的 '"passed"' 形态重叠，须最先判断
        if _REVIEW_GUARDRAIL_MARKER in full_text:
            return FakeProvider._respond_review_guardrail(full_text)
        if _REVIEW_SUPPORT_MARKER in full_text:
            return FakeProvider._respond_review_support(full_text)
        if _REVIEW_META_MARKER in full_text:
            return json.dumps(
                {"summary": "（fake meta-review）各评审员认可方法可溯源，主要不足在实验规模。"},
                ensure_ascii=False,
            )
        if _REVIEW_FACTCHECK_MARKER in full_text:
            return FakeProvider._respond_review_factcheck(full_text)
        if _PAPER_REVIEWER_MARKER in full_text:
            return FakeProvider._respond_paper_reviewer(full_text)
        # Idea 2.0 深耕 marker：prompt 内嵌 goal JSON / wiki 摘录 / 检索结果
        # （会撞 "score"、"out_of_scope"、TL;DR 等通用 marker），须先于它们判断
        if _GOAL_EXPLORE_MARKER in full_text:
            return FakeProvider._respond_goal_explore(full_text)
        if _GOAL_REFINE_MARKER in full_text:
            return FakeProvider._respond_goal_refine(full_text)
        if _PROPOSAL_RELATED_MARKER in full_text:
            return FakeProvider._respond_proposal_related(full_text)
        if _PROPOSAL_DESIGN_MARKER in full_text:
            return FakeProvider._respond_proposal_design(full_text)
        if _PROPOSAL_EXPERIMENTS_MARKER in full_text:
            return FakeProvider._respond_proposal_experiments()
        if _NOVELTY_CHECK_MARKER in full_text:
            return FakeProvider._respond_novelty_check(full_text)
        if _PROPOSAL_RISKS_MARKER in full_text:
            return FakeProvider._respond_proposal_risks()
        if _PROPOSAL_TITLE_MARKER in full_text:
            return FakeProvider._respond_proposal_title(full_text)
        if _PROPOSAL_REVIEW_MARKER in full_text:
            return FakeProvider._respond_proposal_review(full_text)
        if _PROPOSAL_REVISE_MARKER in full_text:
            return json.dumps(
                {
                    "sections": {
                        "design": "（fake revision）已按必须修复清单调整设计。"
                        + "补充了消融与统计检验细节（fake）。" * 5
                    }
                },
                ensure_ascii=False,
            )
        # 伴读/文献库对话 system prompt 会内嵌论文全文/wiki（可能含 TL;DR 等其他
        # marker），须最先判断
        if _LIBRARY_MARKER in full_text:
            return FakeProvider._respond_library(last_user)
        if _READING_MARKER in full_text:
            return FakeProvider._respond_reading(last_user)
        # 论文写作三 marker 内嵌 fact-pack JSON（可能撞其他 marker），须先于通用 JSON 判断
        if _WRITE_REFLECT_MARKER in full_text:
            return FakeProvider._respond_writing_reflect(last_user)
        if _WRITE_RELATED_MARKER in full_text:
            return FakeProvider._respond_writing_related(last_user)
        if _WRITE_SECTION_MARKER in full_text:
            return FakeProvider._respond_writing_section(full_text, last_user)
        if _VERDICT_MARKER in full_text and _PLAN_MARKER not in full_text:
            return json.dumps(
                {"passed": True, "reason": "fake-sextant: 产出满足验收标准（确定性假判定）"},
                ensure_ascii=False,
            )
        # 注意顺序：experiment 计划 prompt 含 '"steps"'（与 navigator 计划 marker 重叠），
        # 且 setup/报告的 user prompt 内嵌 plan JSON（含 "repro_strategy"），
        # 所以 experiment 代码/报告/计划 marker 必须先于 navigator 计划 marker 判断
        if _EXP_CODE_MARKER in full_text:
            return FakeProvider._respond_exp_code()
        if _EXP_REPORT_MARKER in full_text:
            return FakeProvider._respond_exp_report()
        if _EXP_PLAN_MARKER in full_text:
            return FakeProvider._respond_exp_plan()
        if _PLAN_MARKER in full_text:
            return json.dumps(_FAKE_PLAN, ensure_ascii=False)
        if _INTERVIEW_MARKER in full_text:
            return FakeProvider._respond_interview(last_user)
        if _CONCEPTS_MARKER in full_text:
            return FakeProvider._respond_concepts(last_user)
        if _JUDGE_MARKER in full_text:
            return json.dumps(
                {"winner": "a", "reason": "fake-judge：正方论证更充分（确定性假判定）"},
                ensure_ascii=False,
            )
        if _GAPS_MARKER in full_text:
            return FakeProvider._respond_gaps()
        if _IDEAS_MARKER in full_text:
            return FakeProvider._respond_forge_ideas(last_user)
        if _IDEA_SCORE_MARKER in full_text:
            return FakeProvider._respond_idea_scores(last_user)
        if _RELEVANCE_MARKER in full_text:
            return FakeProvider._respond_relevance(last_user)
        if _LIBRARIAN_MARKER in full_text:
            return FakeProvider._respond_librarian(last_user)
        return f"[fake:{model}] {last_user[:400]}"

    @staticmethod
    def _respond_library(last_user: str) -> str:
        """文献库对话：回显问题 + 带 [n] 引用标注的确定性中文回答。"""
        return (
            f"（fake 文献综合）关于「{last_user[:200]}」：综合检索到的文献，"
            "主流做法可以归为两类——一类强调显式规划 [1]，另一类依赖自我反思迭代 [2]。"
            "两者在评测基准上的结论基本一致 [1][2]。文献库中未检索到的内容，我无法确定。"
        )

    @staticmethod
    def _respond_reading(last_user: str) -> str:
        """AI 伴读：回显问题的确定性中文回答（流式分片由 stream() 按 64 字符切）。"""
        return (
            f"（fake 伴读）关于「{last_user[:200]}」：根据论文内容，"
            "该问题的要点如下——方法部分给出了核心设计（fake），实验部分验证了有效性（fake）。"
            "论文中未提及的内容，我无法确定。"
        )

    @staticmethod
    def _respond_interview(last_user: str) -> str:
        """研究方向定义起草：从 prompt 提取 statement / 用户关键词，返回确定性合法草稿。"""
        m = re.search(r"方向定义：(.+)", last_user)
        statement = m.group(1).strip() if m else "研究方向（fake）"
        include: list[str] = []
        idx = last_user.find("用户关键词：")
        start = last_user.find("[", idx)
        end = last_user.find("]", start)
        if idx != -1 and start != -1 and end > start:
            try:
                include = [
                    str(k) for k in json.loads(last_user[start : end + 1]) if isinstance(k, str)
                ]
            except json.JSONDecodeError:
                include = []
        return json.dumps(
            {
                "statement": statement,
                "goals": [
                    f"梳理「{statement}」的研究现状（fake）",
                    "识别关键开放问题并形成研究路线（fake）",
                ],
                "in_scope": [f"与「{statement}」直接相关的方法与评测（fake）"],
                "out_of_scope": ["与该方向无关的一般性综述（fake）"],
                "questions": [
                    "主流方法有哪些？（fake）",
                    "现有方法的局限是什么？（fake）",
                    "如何设计评测验证改进？（fake）",
                ],
                "rubric": [
                    {"name": "relevance", "description": "与方向的相关程度（fake）", "weight": 1.0}
                ],
                "keywords": {
                    "arxiv_categories": ["cs.CL", "cs.LG"],
                    "include": include + ["llm agent (fake)"],
                    "synonyms": {"agent": ["智能体"]},
                },
                "anchor_papers": [],
                "cadence": "daily",
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _respond_relevance(last_user: str) -> str:
        """相关性打分：标题/摘要含 "irrelevant" 判低分，否则高分（确定性、可测）。"""
        score = 0.15 if "irrelevant" in last_user.lower() else 0.88
        return json.dumps(
            {
                "score": score,
                "reason": "fake-relevance: 依据标题/摘要关键词的确定性假打分",
                "tldr": "（fake TL;DR）" + last_user.strip().splitlines()[-1][:120],
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _respond_concepts(last_user: str) -> str:
        """概念定义批量请求：从「概念列表：[...]」提取名称，逐个返回假定义。"""
        names: list[str] = []
        idx = last_user.find(_CONCEPTS_MARKER)
        start = last_user.find("[", idx)
        end = last_user.find("]", start)
        if start != -1 and end > start:
            try:
                parsed = json.loads(last_user[start : end + 1])
                names = [str(n) for n in parsed if isinstance(n, str)]
            except json.JSONDecodeError:
                names = []
        return json.dumps(
            {
                "concepts": [
                    {
                        "name": n,
                        "definition": f"{n} 的一句话定义（fake）",
                        "category": "method",
                    }
                    for n in names
                ]
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _respond_gaps() -> str:
        """forge gap 分析：确定性两条研究空白。"""
        return json.dumps(
            {
                "gaps": [
                    {"title": "研究空白一（fake）", "description": "现有方法缺少 g1 能力（fake）"},
                    {"title": "研究空白二（fake）", "description": "评测体系缺少 g2 维度（fake）"},
                ]
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _respond_forge_ideas(last_user: str) -> str:
        """forge 候选生成：从「生成 N 个」提取数量，返回 N 个互不相似的确定性想法。"""
        m = re.search(r"生成\s*(\d+)\s*个", last_user)
        n = int(m.group(1)) if m else 3
        ideas = [
            {
                "title": f"候选想法 {i}：面向空白 g{i} 的方法（fake）",
                "summary": f"fake-summary-{i}：探索主题 t{i} 的独立路线 token{i}",
                "motivation": f"动机（fake idea {i}）",
                "method": f"方法概述（fake idea {i}）",
                "experiments": f"预期实验（fake idea {i}）",
                "risks": f"风险（fake idea {i}）",
            }
            for i in range(1, n + 1)
        ]
        return json.dumps({"ideas": ideas}, ensure_ascii=False)

    @staticmethod
    def _respond_idea_scores(last_user: str) -> str:
        """forge 四维打分：标题含 "weak" 给低分，否则给高分（确定性、可测）。"""
        low = "weak" in last_user.lower()
        base = (
            {"novelty": 3.0, "feasibility": 4.0, "operability": 3.0, "impact": 2.0}
            if low
            else {
                "novelty": 8.0,
                "feasibility": 7.0,
                "operability": 6.0,
                "impact": 8.0,
            }
        )
        return json.dumps(
            base | {"rationale": {dim: f"fake-rationale：{dim} 的确定性假理由" for dim in base}},
            ensure_ascii=False,
        )

    @staticmethod
    def _respond_exp_plan() -> str:
        """experiment 计划：确定性合法 plan JSON（含 M5-A 必填 primary_metric）。"""
        return json.dumps(
            {
                "hypotheses": [
                    {"text": "假设一：新方法优于基线（fake）", "status": "testing"},
                    {"text": "假设二：合成数据足以验证趋势（fake）", "status": "testing"},
                ],
                "repro_strategy": "用官方基线代码在小型合成数据上复现（fake）",
                "steps": ["准备合成数据（fake）", "训练基线与新方法（fake）", "对比指标（fake）"],
                "primary_metric": {"name": "accuracy", "direction": "maximize"},
                "budget_estimate": {"gpu_hours": 2, "runs": 3},
            },
            ensure_ascii=False,
        )

    def _respond_plan_edit(self) -> str:
        """计划编辑（navigator.on_result）：确定性替代步骤，engine 会自动作废失败节点。"""
        return json.dumps(
            {
                "reason": "失败步骤不可行，改用替代步骤（fake）",
                "edits": [
                    {
                        "op": "add_nodes",
                        "insert_after": None,
                        "nodes": [
                            {
                                "title": "替代步骤（fake）",
                                "action": "llm.complete",
                                "params": {"stage": "navigator", "prompt": "替代执行：{goal}"},
                                "acceptance": "输出包含替代执行的结果",
                                "requires_gate": None,
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        )

    def _respond_exp_reflection(self) -> str:
        """迭代 reflection：调用计数循环 improve → improve → stop（确定性，便于测试）。"""
        seq = getattr(self, "_reflection_calls", 0)
        self._reflection_calls = seq + 1
        if seq % 3 == 2:  # 第 3 次（及每循环第 3 次）：stop + 假设定论
            return json.dumps(
                {
                    "observation": "主指标已稳定，结果足以回答假设（fake）",
                    "diagnosis": "继续迭代收益有限（fake）",
                    "hypothesis_updates": [
                        {"index": 0, "status": "verified", "evidence": "主指标逐轮提升（fake）"},
                        {
                            "index": 1,
                            "status": "falsified",
                            "evidence": "合成数据趋势未复现（fake）",
                        },
                    ],
                    "decision": "stop",
                    "planned_change": None,
                    "stop_reason": "假设已全部有结论（fake）",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "observation": f"第 {seq + 1} 轮运行完成，主指标仍有提升空间（fake）",
                "diagnosis": "学习率偏小导致收敛慢（fake）",
                "hypothesis_updates": [
                    {"index": 0, "status": "testing", "evidence": "趋势向好但未定论（fake）"}
                ],
                "decision": "improve",
                "planned_change": f"增大学习率并延长训练步数（fake round {seq + 1}）",
                "stop_reason": None,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _respond_exp_plot() -> str:
        """experiment 绘图脚本：只读 metrics_all.json 的确定性 matplotlib 脚本。"""
        script = (
            "import json\n"
            "import os\n"
            "import matplotlib\n"
            "matplotlib.use('Agg')\n"
            "import matplotlib.pyplot as plt\n"
            "with open('metrics_all.json', encoding='utf-8') as f:\n"
            "    data = json.load(f)\n"
            "os.makedirs('figures', exist_ok=True)\n"
            "seqs = [r['seq'] for r in data['runs']]\n"
            "values = [r['primary_value'] for r in data['runs']]\n"
            "plt.figure()\n"
            "plt.plot(seqs, values, marker='o', label='primary')\n"
            "plt.xlabel('run seq')\n"
            "plt.ylabel('value')\n"
            "plt.title('primary metric by run')\n"
            "plt.legend()\n"
            "plt.savefig('figures/primary_metric.png')\n"
            "plt.savefig('figures/primary_metric.pdf')\n"
        )
        return json.dumps({"files": {"plot_figures.py": script}}, ensure_ascii=False)

    @staticmethod
    def _respond_exp_code() -> str:
        """experiment 代码生成/修复：含 requirements.txt / run.sh(--smoke) / train.py。"""
        train_py = (
            "import json\n"
            "import sys\n"
            "steps = 1 if '--smoke' in sys.argv else 3\n"
            "for step in range(steps):\n"
            "    value = 0.6 + 0.1 * step\n"
            '    print("POLARIS_METRIC " + json.dumps('
            '{"name": "accuracy", "step": step, "value": value}))\n'
            'print("done (fake experiment)")\n'
        )
        run_sh = (
            "#!/usr/bin/env bash\n"
            "set -e\n"
            'if [ "$1" = "--smoke" ]; then\n'
            "  .venv/bin/python train.py --smoke\n"
            "else\n"
            "  .venv/bin/python train.py\n"
            "fi\n"
        )
        return json.dumps(
            {
                "files": {
                    "requirements.txt": "# no external deps (fake)\n",
                    "run.sh": run_sh,
                    "train.py": train_py,
                }
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _respond_exp_report() -> str:
        return (
            "## 实验报告\n\n"
            "### 结果概览\n\n新方法在合成数据上优于基线（fake）。\n\n"
            "### 指标表现\n\naccuracy 随 step 上升（fake）。\n\n"
            "### 假设验证结论\n\n- 假设一：verified（fake）\n- 假设二：testing（fake）\n\n"
            "### 局限与后续建议\n\n扩大数据规模复验（fake）。\n"
        )

    # ---- M5-B 论文写作（actions_writing.py 三个 system prompt 对齐） ----

    @staticmethod
    def _respond_writing_section(full_text: str, last_user: str) -> str:
        """分节撰写：从 prompt 的 fact-pack JSON 提取合法 bibkey / fig_id / 指标值，
        返回过得了静态校验的正规节文本；测试标记可控地返回违规稿以测拒绝路径。"""
        if "INVALID_CITE_TEST" in full_text:
            return r"Prior work \cite{nonexistent_fake_key} pioneered this direction."
        if "INVALID_FIG_TEST" in full_text:
            return r"\includegraphics[width=\linewidth]{figures/not_a_real_fig.pdf}"
        if "INVALID_NUMBER_TEST" in full_text:
            return "Our method reaches an accuracy of 123.456 on the benchmark."
        bibkey = re.search(r'"bibkey":\s*"([^"]+)"', last_user)
        fig_id = re.search(r'"fig_id":\s*"([^"]+)"', last_user)
        best = re.search(r'"best":\s*(-?\d+(?:\.\d+)?)', last_user)
        section = re.search(r"撰写小节：.*（(\w+)）", last_user)
        lines = [
            f"% fake {section.group(1) if section else 'section'} draft",
            "This section is drafted deterministically by the fake provider.",
        ]
        if bibkey:
            lines.append(rf"Following \cite{{{bibkey.group(1)}}}, we build on established results.")
        if best:
            lines.append(f"The best primary metric reaches {best.group(1)} in our runs.")
        if fig_id:
            lines.append(rf"\includegraphics[width=\linewidth]{{figures/{fig_id.group(1)}.pdf}}")
        return "\n".join(lines)

    @staticmethod
    def _respond_writing_related(last_user: str) -> str:
        """Related Work：引用候选列表的前两个 bibkey（只准从候选集内选引）。"""
        keys = re.findall(r'"bibkey":\s*"([^"]+)"', last_user)[:2]
        if not keys:
            return "No prior work is available in the candidate set (fake)."
        cites = " and ".join(rf"\cite{{{k}}}" for k in keys)
        return f"Closely related studies include {cites}, which we extend (fake related work)."

    @staticmethod
    def _respond_writing_reflect(last_user: str) -> str:
        """self-reflection 精修：确定性回显原文（<<<SECTION ... SECTION>>> 之间）。"""
        m = re.search(r"<<<SECTION\n(.*?)\nSECTION>>>", last_user, re.DOTALL)
        return m.group(1) if m else last_user[:400]

    # ---- M5-C 论文评审（actions_review.py 五个 system prompt 对齐） ----

    @staticmethod
    def _respond_paper_reviewer(full_text: str) -> str:
        """评审员意见：人设不同分（便于聚合测试）；测试标记可控地给低分/坏意见。

        - REVIEW_FAIL_TEST：全员低分 → 评审不通过路径；
        - GUARDRAIL_FAIL_TEST + 人设「严格实验复现者」：意见内嵌
          GUARDRAIL_FAIL_MARKER → guardrail 拒绝 → 重生成 → unreliable 路径。
        """
        profiles = {
            "苛刻方法论者": (6.0, 4.0),
            "建设性领域专家": (8.0, 5.0),
            "严格实验复现者": (7.0, 2.0),  # 低 confidence → 聚合降权路径
        }
        rating, confidence = next(
            (v for name, v in profiles.items() if name in full_text), (7.0, 4.0)
        )
        if "REVIEW_FAIL_TEST" in full_text:
            rating = 3.0
        strengths = ["方法围绕事实包展开，引用与图表可溯源（fake）"]
        if "GUARDRAIL_FAIL_TEST" in full_text and "严格实验复现者" in full_text:
            strengths = ["GUARDRAIL_FAIL_MARKER：论文提出了不存在的定理 9（幻觉，fake）"]
        return json.dumps(
            {
                "soundness": 3.0,
                "presentation": 3.0,
                "contribution": 2.0 if rating <= 3 else 3.0,
                "rating": rating,
                "confidence": confidence,
                "strengths": strengths,
                "weaknesses": ["实验规模有限，缺少更大数据集上的验证（fake）"],
                "questions": ["能否补充关键组件的消融实验？（fake）"],
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _respond_review_guardrail(full_text: str) -> str:
        """guardrail 校验：意见含 GUARDRAIL_FAIL_MARKER → 拒绝（可控失败路径）。"""
        if "GUARDRAIL_FAIL_MARKER" in full_text:
            return json.dumps(
                {"passed": False, "reason": "fake-guardrail：意见含论文中不存在的内容（幻觉）"},
                ensure_ascii=False,
            )
        return json.dumps(
            {"passed": True, "reason": "fake-guardrail：意见具体且引用论文实际内容"},
            ensure_ascii=False,
        )

    @staticmethod
    def _respond_review_support(full_text: str) -> str:
        """引用支撑性判定：语境含 UNSUPPORTED_TEST → unsupported（可控标记）。"""
        support = "unsupported" if "UNSUPPORTED_TEST" in full_text else "supported"
        return json.dumps(
            {"support": support, "reason": "fake-support：依据语境关键词的确定性假判定"},
            ensure_ascii=False,
        )

    @staticmethod
    def _respond_review_factcheck(full_text: str) -> str:
        """claim 抽查：源含 CLAIM_ISSUE_TEST → 报一条 unsupported_claim，否则无问题。"""
        items = []
        if "CLAIM_ISSUE_TEST" in full_text:
            items.append(
                {
                    "location": "results",
                    "issue": "结论超出事实包证据范围（fake）",
                    "evidence": "事实包 metrics 未包含该对比（fake）",
                    "severity": "minor",
                }
            )
        return json.dumps({"items": items}, ensure_ascii=False)

    @staticmethod
    def _respond_librarian(last_user: str, *, with_figure: bool = False) -> str:
        title_match = re.search(r"标题：(.+)", last_user)
        title = title_match.group(1).strip() if title_match else "未知论文"
        # 多模态图文编译（请求带 images）时在方法段落后插入图片标记
        figure_line = "![[fig:0]]\n\n" if with_figure else ""
        return (
            f"## TL;DR\n\n{title} 的一句话总结（fake librarian）。\n\n"
            "## 研究背景与动机\n\n围绕 [[Agent]] 场景的关键问题展开叙述（fake）。\n\n"
            "## 方法\n\n提出基于 [[Agent]] 与 [[强化学习]] 的方法，分段展开讲解（fake）。\n\n"
            f"{figure_line}"
            "## 实验与结果\n\n在多个基准上验证有效（fake）。\n\n"
            "## 讨论与可借鉴点\n\n可复用其训练流程，局限在于评测范围（fake）。\n"
        )

    # ---- Idea 2.0 深耕（actions_proposal.py 的 system prompt 对齐，docs/api-idea2.md） ----

    @staticmethod
    def _respond_goal_explore(full_text: str) -> str:
        """目标构建工具循环：首轮发一次检索；transcript 出现工具结果后交付 goal。

        goal 的 grounding 从 transcript 中出现过的真实库内 paper_id 提取（≤3 篇），
        保证机械验收（validate_goal）可通过；GOAL_INVALID_TEST 标记时故意缺
        research_type 以测重规划路径。
        """
        statement_match = re.search(r"研究方向：(.+)", full_text)
        statement = statement_match.group(1).strip() if statement_match else "研究方向（fake）"
        if "工具结果：" not in full_text and "请立即输出 finish" not in full_text:
            return json.dumps(
                {"tool": "search_papers", "args": {"query": statement[:40], "k": 5}},
                ensure_ascii=False,
            )
        paper_ids = list(dict.fromkeys(re.findall(r'"paper_id": "([0-9a-fA-F-]{36})"', full_text)))[
            :3
        ]
        goal: dict = {
            "research_type": "method",
            "task": f"{statement} 的任务（fake）",
            "question": f"如何改进 {statement}？（fake）",
            "objectives": ["提出新方法并验证有效性（fake）", "给出可复现实验（fake）"],
            "scope": {
                "in_scope": [f"与「{statement}」直接相关的方法（fake）"],
                "out_of_scope": ["无关综述（fake）"],
            },
            "success_criteria": ["主指标超过基线 2 个点（fake）"],
            "grounding": [
                {"paper_id": pid, "why": f"支撑/对比文献 {i}（fake）"}
                for i, pid in enumerate(paper_ids, start=1)
            ],
            "key_concepts": ["Agent", "强化学习"],
            "resources_needed": {
                "compute": "单卡 A100 × 1 周（fake）",
                "data": ["合成数据（公开可得，fake）"],
                "time_weeks": 8,
            },
        }
        if "GOAL_INVALID_TEST" in full_text:
            goal.pop("research_type")
        return json.dumps({"finish": goal}, ensure_ascii=False)

    @staticmethod
    def _respond_goal_refine(full_text: str) -> str:
        """目标修订：回显 <<<GOAL ... GOAL>>> 内的原 goal（并入意见由真实模型做）。"""
        m = re.search(r"<<<GOAL\n(.*?)\nGOAL>>>", full_text, re.DOTALL)
        try:
            goal = json.loads(m.group(1)) if m else {}
        except json.JSONDecodeError:
            goal = {}
        return json.dumps({"goal": goal}, ensure_ascii=False)

    @staticmethod
    def _respond_proposal_related(full_text: str) -> str:
        """相关工作：直接 finish，逐条覆盖「必须覆盖的库内论文 id」（机械验收要求）。"""
        ids: list[str] = []
        m = re.search(r"必须覆盖的库内论文 id：(\[[^\]]*\])", full_text)
        if m:
            try:
                ids = [str(i) for i in json.loads(m.group(1))]
            except json.JSONDecodeError:
                ids = []
        lines = [f"- [[paper:{pid}]] 与本工作的差异：切入角不同（fake）" for pid in ids]
        content = (
            "已有工作围绕该方向展开（fake related work）。\n\n"
            + "\n".join(lines)
            + "\n\n### 本工作与已有工作的差异\n\n"
            + "- 差异一：目标更聚焦（fake）\n- 差异二：评测更完备（fake）"
        )
        return json.dumps({"finish": {"content": content, "extra_papers": []}}, ensure_ascii=False)

    @staticmethod
    def _respond_proposal_design(full_text: str) -> str:
        """方案设计：确定性 markdown；带回炉诊断时输出修订版（novelty 假判定联动）。"""
        revised = "（fake 修订设计）" if "上一版设计未通过" in full_text else ""
        return (
            f"{revised}### 方法总体设计\n\n基于 [[Agent]] 的两阶段方法：规划阶段分解目标，"
            "执行阶段带自验证闭环逐步推进（fake design）。\n\n"
            "### 关键创新点\n\n与已有方法的本质差异在于把验收标准显式建模为可机械检查的约束，"
            "失败时携带诊断信息回传规划器（fake）。\n\n"
            "### 理论依据\n\n在验收判据可判定的前提下，收敛性可由既有结果推出（fake）。\n\n"
            "### 适用边界\n\n适用于可形式化验收的任务；开放式创作类任务不在适用范围（fake）。"
        )

    @staticmethod
    def _respond_proposal_experiments() -> str:
        return json.dumps(
            {
                "content": (
                    "### Baselines\n\n基线 A/B（fake）。\n\n"
                    "### Datasets\n\n合成数据 + 公开基准（fake）。\n\n"
                    "### Metrics\n\n主指标 accuracy（maximize，fake）。\n\n"
                    "### Ablations\n\n逐组件消融（fake）。\n\n"
                    "### 算力粗估\n\n单卡 A100 约 40 GPU 时（fake）。"
                ),
                "smoke_plan": {
                    "goal": "验证核心假设方向是否可行（fake）",
                    "steps": ["构造 100 条合成样例（fake）", "跑最小对照实验（fake）"],
                    "metric": "accuracy",
                    "expected_signal": "新方法高于基线即方向可行（fake）",
                    "est_hours": 8,
                },
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _respond_novelty_check(full_text: str) -> str:
        """新颖性三档：默认 novel；测试标记 + 未见「（fake 修订设计）」时给差评路径。"""
        verdict = "novel"
        if "（fake 修订设计）" not in full_text:
            if "NOVELTY_DUP_TEST" in full_text:
                verdict = "duplicate"
            elif "NOVELTY_DIFF_TEST" in full_text:
                verdict = "needs_differentiation"
        titles = re.findall(r'"title": "([^"]{4,80})"', full_text)[:3]
        comparisons = [
            {"title": t, "difference": "本方案聚焦点与其不同（fake 差异论证）"} for t in titles
        ]
        return json.dumps(
            {
                "verdict": verdict,
                "comparisons": comparisons,
                "reason": f"fake-novelty：确定性假判定（{verdict}）",
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _respond_proposal_risks() -> str:
        return json.dumps(
            {
                "risks": [
                    {
                        "risk": "合成数据结论不外推（fake）",
                        "mitigation": "补充真实基准验证（fake）",
                    },
                    {
                        "risk": "算力不足导致消融不全（fake）",
                        "mitigation": "优先级排序 + 小模型代理（fake）",
                    },
                ]
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _respond_proposal_title(full_text: str) -> str:
        statement_match = re.search(r'"task": "([^"]+)"', full_text)
        task = statement_match.group(1) if statement_match else "研究任务（fake）"
        return json.dumps(
            {
                "title": f"研究方案（fake）：{task[:80]}",
                "summary": "一句话概述（fake proposal）",
                "expected": "论文一篇、开源代码与可复现实验（fake）",
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _respond_proposal_review(full_text: str) -> str:
        """专职评审员：人设不同分；PROPOSAL_MUSTFIX_TEST 时永远给 must_fix（遗留路径）。"""
        profiles = {
            "新颖性评审员": 8.0,
            "方法论评审员": 7.0,
            "可行性评审员": 7.5,
            "影响力评审员": 8.5,
        }
        score = next((v for name, v in profiles.items() if name in full_text), 7.0)
        must_fix = (
            ["fake 必须修复：实验缺少显著性检验"] if "PROPOSAL_MUSTFIX_TEST" in full_text else []
        )
        return json.dumps(
            {
                "score": score,
                "must_fix": must_fix,
                "suggestions": ["fake 建议：补充失败案例分析"],
            },
            ensure_ascii=False,
        )
