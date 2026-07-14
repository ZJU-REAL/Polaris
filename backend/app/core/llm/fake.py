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
_EXP_CODE_MARKER = '"requirements.txt"'  # experiment 代码生成/修复
_EXP_REPORT_MARKER = "## 实验报告"  # experiment 报告
_READING_MARKER = "论文阅读助手"  # AI 伴读（papers.py CHAT_SYSTEM_PROMPT_TEMPLATE）

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
        if images and _LIBRARIAN_MARKER in full_text:
            # 多模态图文编译：librarian markdown 里插入一行 ![[fig:0]] 图片标记
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            content = self._respond_librarian(last_user, with_figure=True)
        elif images:
            # 多模态（论文图筛选注释）：确定性选前两张图并配假图注
            content = json.dumps(
                [
                    {"index": i, "important": True, "caption": "（fake）图注"}
                    for i in range(min(2, len(images)))
                ],
                ensure_ascii=False,
            )
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
        # 伴读 system prompt 会内嵌论文全文/wiki（可能含 TL;DR 等其他 marker），须最先判断
        if _READING_MARKER in full_text:
            return FakeProvider._respond_reading(last_user)
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
        """experiment 计划：确定性合法 plan JSON（hypotheses/repro/steps/budget_estimate）。"""
        return json.dumps(
            {
                "hypotheses": [
                    {"text": "假设一：新方法优于基线（fake）", "status": "testing"},
                    {"text": "假设二：合成数据足以验证趋势（fake）", "status": "testing"},
                ],
                "repro_strategy": "用官方基线代码在小型合成数据上复现（fake）",
                "steps": ["准备合成数据（fake）", "训练基线与新方法（fake）", "对比指标（fake）"],
                "budget_estimate": {"gpu_hours": 2, "runs": 3},
            },
            ensure_ascii=False,
        )

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

    @staticmethod
    def _respond_librarian(last_user: str, *, with_figure: bool = False) -> str:
        title_match = re.search(r"标题：(.+)", last_user)
        title = title_match.group(1).strip() if title_match else "未知论文"
        # 多模态图文编译（请求带 images）时在方法段落后插入图片标记
        figure_line = "![[fig:0]]\n\n" if with_figure else ""
        return (
            f"## TL;DR\n\n{title} 的一句话总结（fake librarian）。\n\n"
            "## 研究动机\n\n围绕 [[Agent]] 场景的关键问题展开（fake）。\n\n"
            "## 方法\n\n提出基于 [[Agent]] 与 [[强化学习]] 的方法（fake）。\n\n"
            f"{figure_line}"
            "## 实验结论\n\n在多个基准上验证有效（fake）。\n\n"
            "## 可借鉴点\n\n- 可复用其训练流程（fake）\n\n"
            "## 相关概念\n\n[[Agent]] · [[强化学习]]\n"
        )
