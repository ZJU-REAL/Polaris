"""假设/实验树：discovery run 的产品本体（设计报告 §8.2，P2 D1）。

Navigator 在树上做分支/剪枝决策而非线性 plan：每个节点是一条假设（或它派生的
实验/分析），带文献锚定（grounding）、查新（novelty_report）、可行性（feasibility）
与评分；恢复语义 = 从最优未扩展（open）节点继续。本模块只定义实体，树的写入
由引擎侧（D2/D3）经 services/hypothesis_tree.py 进行。
"""

import uuid
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin

# 节点种类：hypothesis 假设本体；experiment/analysis 是它派生的验证动作节点
HYPOTHESIS_NODE_KINDS = ("hypothesis", "experiment", "analysis")

# 状态机：open →（expanded | pruned）；expanded →（validated | refuted | pruned）
# validated / refuted / pruned 均为终态——被剪枝分支强制留痕（防择优汇报，§8.2）
HYPOTHESIS_NODE_STATUSES = ("open", "expanded", "pruned", "validated", "refuted")


class HypothesisNode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "hypothesis_nodes"
    # 两个读路径都按 (run_id, seq) 取：树读取全量排序、恢复取最优里的最早一个
    __table_args__ = (Index("ix_hypothesis_nodes_run_seq", "run_id", "seq"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("voyage_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 自引用父节点；NULL = 树根（每 run 单根，由服务层保证）。防环无需额外机制：
    # 创建时 parent 必须已存在且属同 run，新节点不可能成为任何既有节点的祖先。
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("hypothesis_nodes.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # HYPOTHESIS_NODE_KINDS
    statement: Mapped[str] = mapped_column(Text, nullable=False)  # 假设陈述
    # 子命题→文献绑定表 [{subclaim, stance: support|refute|speculation,
    #                     paper_ids: [...], snippets: [...]}]
    grounding: Mapped[list[Any] | None] = mapped_column(JSONVariant)
    novelty_report: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)  # 查新（逐子命题）
    feasibility: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)  # 资源匹配结果
    score: Mapped[float | None] = mapped_column(Float)  # Navigator 排序依据
    status: Mapped[str] = mapped_column(
        String(16), default="open", index=True, nullable=False
    )  # HYPOTHESIS_NODE_STATUSES
    #: run 内的创建序号（1 起）。存在的理由只有一个：**顺序不能靠墙钟**。
    #:
    #: 恢复语义（best_open_node）说「同分取先建」，而 created_at 是 Python 侧
    #: ``utcnow()``——不单调。NTP 步进或回拨会让后建的节点拿到更小的时间戳，
    #: 于是「先建」选出的是后建的那个；同一微秒并列时更是完全无序。两种情况都不
    #: 报错，只是这次恢复走进了另一棵子树。
    #:
    #: 按 run 计数而不是全局自增：序号的意义是「这棵树里的第几个」，跨 run 连续
    #: 只会让读日志的人误以为两个 run 之间有关系。
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
