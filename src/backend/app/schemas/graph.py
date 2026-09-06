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


# —— 未连接概念对（P2.5 F3，Swanson ABC；服务见 services/concept_fuels.py） ——


class ConceptPairNode(BaseModel):
    """候选对 / 桥概念的最小引用（uuid 字符串 + 名字，点击跳概念详情够用）。"""

    id: str
    name: str


class ConceptPairBridge(ConceptPairNode):
    strength: int  # 该桥的 min(w(A,B), w(B,C))


class UnconnectedConceptPair(BaseModel):
    """A–C 从未同篇出现、但经共同邻居相连的概念对；strength = Σ_B min(w(A,B), w(B,C))。"""

    concept_a: ConceptPairNode
    concept_c: ConceptPairNode  # 规范序：concept_a.id < concept_c.id
    strength: int
    bridges: list[ConceptPairBridge]  # 最强的 ≤5 个桥概念，按强度降序
