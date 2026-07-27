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
