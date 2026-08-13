"""方向画像：statement 与关键词如何组进「打分依据」和「粗排查询」（确定性单元测试）。"""

from app.services.relevance import build_direction_query, build_relevance_context

DEFINITION = {
    "statement": "研究长时程智能体",
    "keywords": {"include": ["Long-Running Agent", "记忆压缩", "long-running agent", " "]},
    "rubric": ["方法新颖性"],
    "questions": ["如何评估长期一致性"],
}


def test_relevance_context_includes_keywords():
    """关键词要给模型看：一句话方向描述不是人人写得好，意图常常主要落在关键词上。"""
    text = build_relevance_context(DEFINITION, "兜底库名")
    assert "研究长时程智能体" in text
    assert "Long-Running Agent" in text
    assert "记忆压缩" in text
    assert "方法新颖性" in text
    assert "如何评估长期一致性" in text


def test_direction_query_is_statement_plus_keywords_only():
    """粗排查询只要能代表「研究什么」：带上关键词，但不掺 rubric。

    rubric 讲的是「怎么打分」，混进去会把向量拉偏。
    """
    query = build_direction_query(DEFINITION, "兜底库名")
    assert "研究长时程智能体" in query
    assert "Long-Running Agent" in query
    assert "方法新颖性" not in query
    assert "如何评估长期一致性" not in query


def test_keywords_deduped_case_insensitively_and_blanks_dropped():
    query = build_direction_query(DEFINITION, "兜底库名")
    assert query.lower().count("long-running agent") == 1  # 大小写重复只留一个
    assert "、、" not in query  # 空白项不产生空槽


def test_falls_back_to_library_name_without_statement():
    assert "某个库" in build_direction_query({"keywords": {"include": ["RAG"]}}, "某个库")
    assert "RAG" in build_direction_query({"keywords": {"include": ["RAG"]}}, "某个库")
    # 什么都没有也不该炸
    assert build_direction_query(None, "") == ""


# ---- TL;DR 是论文自己的，不是对某个库的判词 ----


def test_prompt_separates_the_verdict_from_the_summary():
    """提示词必须把 reason 和 tldr 分开，并明说 tldr 不许提方向。

    生产上 papers.tldr 长这样：「论文关注延迟反馈下策略评估的诊断方法，不涉及长期运行
    智能体的自主性、记忆或持续执行，相关性较低。」——那是对某一个库的判词，却挂在
    全平台共享的论文上。整段提示词都在讲「对照研究方向评估」，不明确要求，模型给出
    的自然就是判词。
    """
    from app.services.relevance import RELEVANCE_SYSTEM_PROMPT

    assert "不要提研究方向" in RELEVANCE_SYSTEM_PROMPT
    assert "相关性较低" in RELEVANCE_SYSTEM_PROMPT  # 明确点名不许写成这种句子
    assert "全平台只有一份" in RELEVANCE_SYSTEM_PROMPT


def test_scoring_only_fills_an_empty_tldr():
    """打分只在 tldr 为空时填，绝不覆盖。

    覆盖的后果是「谁最后打分谁说了算」：A 库打完是 A 的判词，B 库一打分就换成 B 的，
    而两者都会被第三个库、每日流和搜索结果当成这篇论文的摘要显示。
    """
    import inspect

    from app.services import relevance

    src = inspect.getsource(relevance.score_paper_relevance)
    assert "paper.tldr = paper.tldr or " in src, "必须是「空了才填」，不能反过来"


def test_compiled_wiki_becomes_the_authoritative_tldr():
    """编译出的 ## TL;DR 要写回 paper.tldr。

    编译提示词不带任何库的方向陈述或 rubric，所以它对全平台是同一份——这正是
    「一篇论文一份解读」想要的东西。不写回的话，papers.tldr 就只剩打分阶段那个
    带方向色彩的占位。
    """
    import inspect

    from app.services import paper_wiki

    src = inspect.getsource(paper_wiki.upsert_wiki)
    assert "extract_tldr" in src
    assert "paper.tldr = compiled_tldr" in src
