"""discovery run 的披露报告组装（#655，设计报告 §8.2 防失败模式，P2 D6）。

把一次假设树探索「实际做了什么」从 run 的既有记录里**确定性**拼出来——零 LLM
调用、零新增判断：检索留痕在 checkpoint 轮次账本（#655 起 expand 记入）、剪枝
原因在决策留痕、对阵在 tournament 账本、token 在 node_usage，节点证据在树上。
披露的价值恰恰在于它只是既有事实的重排：报告说的每一句都能指回一条原始记录。

产物形状（artifacts["discovery-disclosure.json"]，summarize 时落盘）::

    {
      "version": 1,
      "queries":  [{round, phase: generate|ground|novelty, node_id, query,
                    paper_ids}],                       # 检索了什么（全录）
      "papers":   {retrieved, cited, retrieved_not_cited, cited_not_retrieved},
      "branches": [{node_id, parent_id, statement, status, score,
                    timeline: [{event: created|expanded|pruned, round,
                                reason?, cascade_from?}]}],
      "tournament": {matches, nodes},                  # 深度模式的对阵全录
      "accounting": {node_usage, total},               # 每节点 + 总量 token
      "invariants": {cited_subset_of_retrieved, pruned_have_reasons},
      "warnings":  [{code, ...}],                      # 不变量违反时如实记录
    }

不变量违反**记 warnings 而不抛错**（§8.2）：披露是对既有 run 的如实陈述，run
本身有伤（旧版本没留检索痕、断点补账丢了归属）时，报告该把伤口说出来，而不是
因为伤口存在就拒绝出报告。
"""

from collections.abc import Iterable, Sequence
from typing import Any

# 轮次账本里合法不带检索留痕的记录标志（无事发生 / 断点补账）
_NO_TRACE_FLAGS = ("no_open", "replayed_from_tree")


def _add_unique(out: list[str], ids: Iterable[Any]) -> None:
    for raw in ids:
        pid = str(raw)
        if pid and pid not in out:
            out.append(pid)


def _sorted_rounds(rounds: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """轮次账本按轮号升序（键是字符串数字；坏键按 0 处理，不因脏数据拒绝出报）。"""

    def _no(key: str) -> int:
        try:
            return int(key)
        except (TypeError, ValueError):
            return 0

    return sorted(
        ((_no(k), v) for k, v in rounds.items() if isinstance(v, dict)),
        key=lambda kv: kv[0],
    )


def build_disclosure(checkpoint: dict[str, Any], nodes: Sequence[Any]) -> dict[str, Any]:
    """checkpoint + 假设树节点 → 披露 JSON（纯确定性，见模块 docstring）。

    传 checkpoint 而不是 run：summarize 调用时最新的账本在 ctx.checkpoint 内存里
    （engine 在动作结束后才回写 run.checkpoint，动作中途读 run 拿到的是旧账）。
    nodes 按 tree_for_run 的创建序传入，只读 id/parent_id/statement/status/score/
    grounding/novelty_report——测试可用轻量替身，不必过数据库。
    """
    state = checkpoint.get("discovery") if isinstance(checkpoint, dict) else None
    state = state if isinstance(state, dict) else {}
    rounds = state.get("rounds") if isinstance(state.get("rounds"), dict) else {}
    decisions = [d for d in (state.get("decisions") or []) if isinstance(d, dict)]
    node_usage = (
        state.get("node_usage") if isinstance(state.get("node_usage"), dict) else {}
    )
    tournament = (
        state.get("tournament") if isinstance(state.get("tournament"), dict) else {}
    )
    warnings: list[dict[str, Any]] = []

    # ---- queries：轮次账本里的检索留痕拉平（按轮序，轮内保持记录顺序） ----
    queries: list[dict[str, Any]] = []
    retrieved: list[str] = []
    for round_no, record in _sorted_rounds(rounds):
        for raw in record.get("queries") or []:
            if not isinstance(raw, dict):
                continue
            paper_ids: list[str] = []
            _add_unique(paper_ids, raw.get("paper_ids") or [])
            queries.append(
                {
                    "round": round_no,
                    "phase": str(raw.get("phase") or ""),
                    "node_id": raw.get("node_id"),
                    "query": str(raw.get("query") or ""),
                    "paper_ids": paper_ids,
                }
            )
            _add_unique(retrieved, paper_ids)
        inspirations = record.get("inspiration_paper_ids")
        if isinstance(inspirations, dict):
            # 引文远端/多样窗两路没有查询文本，但论文确实被读过——计入 retrieved
            for route_ids in inspirations.values():
                _add_unique(retrieved, route_ids or [])
        elif record.get("node_id") is not None or not any(
            record.get(flag) for flag in _NO_TRACE_FLAGS
        ):
            # 真扩展过却没有检索留痕：旧版本 run（#655 之前）或脏账，如实标注
            warnings.append({"code": "round_without_trace", "round": round_no})

    # ---- papers：retrieved 全集 vs 实引（grounding 立场引用 + 查新引用） ----
    cited: list[str] = []
    for node in nodes:
        grounding = node.grounding if isinstance(node.grounding, list) else []
        for entry in grounding:
            if isinstance(entry, dict) and entry.get("stance") in ("support", "refute"):
                _add_unique(cited, entry.get("paper_ids") or [])
        report = node.novelty_report if isinstance(node.novelty_report, dict) else {}
        for entry in report.get("subclaims") or []:
            if isinstance(entry, dict):
                _add_unique(cited, entry.get("paper_ids") or [])
    retrieved_set = set(retrieved)
    cited_not_retrieved = [pid for pid in cited if pid not in retrieved_set]
    if cited_not_retrieved:
        # 引用只该来自检索结果（管线的结构性约束）：违反说明留痕缺失或数据有伤
        warnings.append(
            {"code": "cited_not_retrieved", "paper_ids": cited_not_retrieved}
        )
    papers = {
        "retrieved": retrieved,
        "cited": cited,
        "retrieved_not_cited": [pid for pid in retrieved if pid not in set(cited)],
        "cited_not_retrieved": cited_not_retrieved,
    }

    # ---- branches：每节点 created/expanded/pruned 时间线（决策留痕反查原因） ----
    created_round: dict[str, int] = {}
    expanded_round: dict[str, int] = {}
    for round_no, record in _sorted_rounds(rounds):
        for child_id in record.get("children") or []:
            created_round.setdefault(str(child_id), round_no)
        if record.get("node_id"):
            expanded_round.setdefault(str(record["node_id"]), round_no)
    prune_decisions: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        if decision.get("decision") == "pruned" and decision.get("node_id"):
            prune_decisions.setdefault(str(decision["node_id"]), decision)
    parent_of = {
        str(n.id): (str(n.parent_id) if n.parent_id else None) for n in nodes
    }
    status_of = {str(n.id): n.status for n in nodes}

    def _cascade_source(node_id: str) -> str | None:
        """沿祖先找最近一个「有直接剪枝决策」的被剪节点（级联剪枝的来源）。"""
        seen = {node_id}
        current = parent_of.get(node_id)
        while current is not None and current not in seen:
            seen.add(current)
            if status_of.get(current) == "pruned" and current in prune_decisions:
                return current
            current = parent_of.get(current)
        return None

    branches: list[dict[str, Any]] = []
    pruned_have_reasons = True
    for node in nodes:
        node_id = str(node.id)
        # 根节点是 seed 建的（第 0 轮）；其余节点的出生轮查账本 children 列表，
        # 查不到（断点补账丢了名单）就如实记 None
        born = 0 if parent_of.get(node_id) is None else created_round.get(node_id)
        timeline: list[dict[str, Any]] = [{"event": "created", "round": born}]
        if node_id in expanded_round:
            timeline.append({"event": "expanded", "round": expanded_round[node_id]})
        if node.status == "pruned":
            decision = prune_decisions.get(node_id)
            if decision is not None:
                timeline.append(
                    {
                        "event": "pruned",
                        "round": decision.get("round"),
                        "reason": str(decision.get("why") or ""),
                    }
                )
            else:
                source = _cascade_source(node_id)
                if source is not None:
                    timeline.append(
                        {
                            "event": "pruned",
                            "round": prune_decisions[source].get("round"),
                            "reason": "随父分支级联剪枝",
                            "cascade_from": source,
                        }
                    )
                else:
                    # 不变量违反：被剪却查无原因（直接决策没有、级联来源也没有）
                    pruned_have_reasons = False
                    warnings.append(
                        {"code": "pruned_without_reason", "node_id": node_id}
                    )
                    timeline.append({"event": "pruned", "round": None, "reason": None})
        branches.append(
            {
                "node_id": node_id,
                "parent_id": parent_of.get(node_id),
                "statement": node.statement,
                "status": node.status,
                "score": node.score,
                "timeline": timeline,
            }
        )

    # ---- accounting：每节点账本原样归档 + 总量 ----
    total = {"prompt_tokens": 0, "completion_tokens": 0}
    for entry in node_usage.values():
        if isinstance(entry, dict):
            total["prompt_tokens"] += int(entry.get("prompt_tokens", 0) or 0)
            total["completion_tokens"] += int(entry.get("completion_tokens", 0) or 0)

    return {
        "version": 1,
        "queries": queries,
        "papers": papers,
        "branches": branches,
        "tournament": {
            "matches": tournament.get("matches") or [],
            "nodes": tournament.get("nodes") or {},
        },
        "accounting": {"node_usage": node_usage, "total": total},
        "invariants": {
            "cited_subset_of_retrieved": not cited_not_retrieved,
            "pruned_have_reasons": pruned_have_reasons,
        },
        "warnings": warnings,
    }
