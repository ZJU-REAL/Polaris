"""知识图谱 schema：论文 / 作者 / 概念节点与其关联边。"""

from typing import Literal

from pydantic import BaseModel

GraphNodeType = Literal["paper", "concept", "author"]
GraphEdgeKind = Literal["paper_concept", "paper_author"]


class GraphNode(BaseModel):
    id: str  # paper/concept 为 uuid 字符串，author 为 "author:<slug>"
    type: GraphNodeType
    label: str
    # —— 按类型可选的展示字段 ——
    status: str | None = None  # paper
    year: int | None = None  # paper
    published: str | None = None  # paper 发表日期（ISO date；时间线按月分组用）
    relevance: float | None = None  # paper
    category: str | None = None  # concept
    count: int = 1  # author/concept 关联论文数（决定节点大小）


class GraphEdge(BaseModel):
    source: str
    target: str
    kind: GraphEdgeKind


class GraphResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    paper_total: int  # 项目内符合条件的论文总数
    truncated: bool  # 超出上限被截断（按相关度保留 top N）
