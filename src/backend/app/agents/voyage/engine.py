"""VoyageEngine：持久化状态机，驱动 Navigator/Helm/Sextant 三元组闭环。

任务循环（docs/voyage-loop.md）：计划载体是**带状态位的扁平步骤清单**（真源在
voyage_steps 行，run.plan 只是派生快照），调度规则 = 按 rank 取第一个非终态节点。

    planning → executing → verifying ─┬→ (下一节点) executing
                                      ├→ 原地重试（执行类错误，attempt < max）
                                      ├→ replanning → executing（template/loop）
                                      ├→ paused_gate（审批后 resume）
                                      ├→ paused_error（pipeline 失败/重规划超限，可修复后重试）
                                      └→ done / failed
要点：
- 失败分派按 run.mode（docs/voyage-loop.md §5.1）：
  执行类错误（observation.error）在节点 max_attempts 内带诊断原地重试；
  判断类失败（校验未过）不重试——pipeline 直接停（on_failure="fail" → failed，
  否则 paused_error 等人工修复后断点重试），template/loop 走重规划；
- 重规划不再删行：旧尾部节点标 obsolete 留痕，新节点按 rank 间隙追加（seq 只增不改）；
- 每次尝试完整归档进 step.attempts（SSE 事件不持久，审计留痕一律落库）；
- requires_gate 步骤执行前创建 Gate 并暂停（结束本次 ARQ 任务），approve 后
  由 resume_voyage 续跑；resume 会把 paused_error 的失败节点复位重试；
- cancel 协作式：每步开始前查 DB status，状态写入用条件 UPDATE 防覆盖 cancelled；
- 全程向 Redis ``voyage:{id}:events`` 发布 status/step/log 事件；
- 步骤与 Sextant 的 tokens 累加到 run.usage，超出 budget.max_tokens 则暂停。
"""

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.voyage.actions import ActionContext
from app.agents.voyage.checks import run_deterministic_checks
from app.agents.voyage.helm import Helm
from app.agents.voyage.navigator import Navigator, NavigatorError, done_criteria_for_kind
from app.agents.voyage.plan_edit import SIGNAL_TABLES, PlanEditError
from app.agents.voyage.sextant import Sextant
from app.core.db import get_sessionmaker
from app.core.events import EventBus
from app.core.llm.router import LLMRouter, get_llm_router
from app.models.base import utcnow
from app.models.gate import Gate
from app.models.llm_config import LLMUsage
from app.models.voyage import TERMINAL_STATUSES, VoyageRun, VoyageStep, mode_for_kind
from app.services import skills as skills_service

MAX_REPLANS = 2
_RANK_GAP = 100.0


class _ExternallyTerminated(Exception):
    """状态被外部置为终态（如用户 cancel），本次驱动直接退出。"""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


def _serialize_step(step: VoyageStep) -> dict[str, Any]:
    return {
        "id": str(step.id),
        "seq": step.seq,
        "rank": step.rank,
        "title": step.title,
        "action": step.action,
        "params": step.params,
        "acceptance": step.acceptance,
        "requires_gate": step.requires_gate,
        "provenance": step.provenance,
        "observation": step.observation,
        "verdict": step.verdict,
        "status": step.status,
        "attempt": step.attempt,
        "attempts": step.attempts,
        "tokens": step.tokens,
        "started_at": step.started_at.isoformat() if step.started_at else None,
        "finished_at": step.finished_at.isoformat() if step.finished_at else None,
    }


def _step_def_from_row(row: VoyageStep, plan: list[Any] | None) -> dict[str, Any]:
    """从节点行重建步骤定义；迁移前的存量行缺列时回退到 run.plan 快照。"""
    fallback: dict[str, Any] = {}
    if isinstance(plan, list) and row.seq < len(plan) and isinstance(plan[row.seq], dict):
        fallback = plan[row.seq]
    acc = row.acceptance if isinstance(row.acceptance, dict) else {}
    provenance = row.provenance if isinstance(row.provenance, dict) else {}
    return {
        "title": row.title,
        "action": row.action,
        "params": row.params or {},
        "acceptance": acc.get("text") if "text" in acc else fallback.get("acceptance"),
        "checks": acc.get("checks") if "checks" in acc else fallback.get("checks"),
        "requires_gate": (
            row.requires_gate if row.requires_gate is not None else fallback.get("requires_gate")
        ),
        "on_failure": provenance.get("on_failure", fallback.get("on_failure")),
        "wrapup": provenance.get("wrapup", fallback.get("wrapup")),
    }


def _is_wrapup(step_def: dict[str, Any]) -> bool:
    """收尾步骤：把已完成工作变成产出的廉价终步（summarize/report/终编译等）。

    预算耗尽时收尾步骤仍放行、其余未执行步骤作废（docs/voyage-loop.md §5.4 降级收尾），
    避免昂贵工作已完成却卡在最后一步 paused_error 而白费。
    """
    return bool(step_def.get("wrapup"))


class VoyageEngine:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        event_bus: EventBus | None = None,
        llm_router: LLMRouter | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker or get_sessionmaker()
        self._bus = event_bus
        llm = llm_router or get_llm_router()
        # 注入事件总线：长文本 stage 的 LLM 调用自动流式广播 token 增量到任务终端
        llm.event_bus = event_bus
        self._llm = llm
        self.navigator = Navigator(llm)
        self.helm = Helm()
        self.sextant = Sextant(llm)

    # ---- 入口 ----

    async def run(self, run_id: uuid.UUID) -> None:
        """首次驱动：无 plan 则先规划，再进入执行循环。"""
        await self._drive(run_id)

    async def resume(self, run_id: uuid.UUID) -> None:
        """闸门审批 / paused_error 重试 / worker 重启后从断点续跑。

        失败节点复位重试（docs/voyage-loop.md：人工修复代码后从断点续跑，
        前功不作废）：attempt 清零（历史已归档进 attempts），状态回 pending。
        """
        async with self._sessionmaker() as session:
            run = await session.get(VoyageRun, run_id)
            if run is None or run.status in TERMINAL_STATUSES:
                return
            stmt = (
                select(VoyageStep)
                .where(VoyageStep.run_id == run_id, VoyageStep.status.in_(("failed", "running")))
                .order_by(VoyageStep.rank, VoyageStep.seq)
            )
            for row in (await session.execute(stmt)).scalars().all():
                row.status = "pending"
                row.attempt = 0
            await session.commit()
        await self._drive(run_id)

    # ---- 事件发布 ----

    async def _emit_voyage(self, run_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
        if self._bus is not None:
            await self._bus.publish_voyage_event(run_id, event, data)

    async def _emit_notify(
        self, project_id: uuid.UUID | None, message: dict[str, Any]
    ) -> None:
        # P9a：库化任务可能无起源课题（project_id 为空）——无项目通知频道，静默跳过。
        if project_id is not None and self._bus is not None:
            await self._bus.publish_notify(project_id, message)

    async def _emit_status(self, run: VoyageRun) -> None:
        await self._emit_voyage(run.id, "status", {"status": run.status, "cursor": run.cursor})
        await self._emit_notify(
            run.project_id,
            {"type": "voyage.status", "voyage_id": str(run.id), "status": run.status},
        )

    async def _emit_step(self, run: VoyageRun, step: VoyageStep) -> None:
        await self._emit_voyage(run.id, "step", {"step": _serialize_step(step)})

    async def _emit_log(
        self,
        run: VoyageRun,
        message: str,
        *,
        level: str = "info",
        step_id: uuid.UUID | str | None = None,
    ) -> None:
        """结构化日志事件（任务详情页 terminal 消费）。

        level: info | step（步骤开始）| success（通过/完成）| error（失败/暂停）|
        plan（计划调整）| budget（预算收尾）| gate（人工审批）——前端据此上色。
        step_id 关联到某步骤（可点/高亮）。旧订阅端只读 message，向后兼容。
        """
        data: dict[str, Any] = {"message": message, "level": level, "at": utcnow().isoformat()}
        if step_id is not None:
            data["step_id"] = str(step_id)
        await self._emit_voyage(run.id, "log", data)

    # ---- 状态持久化 ----

    async def _current_db_status(self, session: AsyncSession, run_id: uuid.UUID) -> str:
        stmt = select(VoyageRun.status).where(VoyageRun.id == run_id)
        return (await session.execute(stmt)).scalar_one()

    async def _set_status(self, session: AsyncSession, run: VoyageRun, status: str) -> None:
        """条件 UPDATE：绝不覆盖外部写入的 cancelled（协作式取消）。"""
        stmt = (
            update(VoyageRun)
            .where(VoyageRun.id == run.id, VoyageRun.status != "cancelled")
            .values(status=status)
        )
        result = await session.execute(stmt)
        await session.commit()
        if result.rowcount == 0:
            run.status = await self._current_db_status(session, run.id)
            raise _ExternallyTerminated(run.status)
        run.status = status
        await self._emit_status(run)

    # ---- 主流程 ----

    async def _drive(self, run_id: uuid.UUID) -> None:
        async with self._sessionmaker() as session:
            run = await session.get(VoyageRun, run_id)
            if run is None or run.status in TERMINAL_STATUSES:
                return
            try:
                # mode 由 kind 静态决定（docs/voyage-loop.md §2）；首次驱动时对齐，
                # 顺带覆盖迁移前创建的存量 run
                expected_mode = mode_for_kind(run.kind)
                if run.mode != expected_mode:
                    run.mode = expected_mode
                    await session.commit()
                await self._ensure_skills_snapshot(session, run)
                if run.plan is None:
                    await self._plan(session, run)
                await self._ensure_step_rows(session, run)
                if run.status == "planning":
                    await self._set_status(session, run, "executing")
                await self._loop(session, run)
            except _ExternallyTerminated:
                # 外部 cancel：补发一次终态事件后安静退出
                await self._emit_status(run)

    async def _ensure_skills_snapshot(self, session: AsyncSession, run: VoyageRun) -> None:
        """首次驱动时把项目生效技能内容快照进 checkpoint["skills"]（docs/skill-system.md §3.2）。

        此后本次 run 只读快照：中途改技能不影响进行中任务，断点恢复无需再查技能表，
        且事后可回放「本次任务用了哪些技能的哪个版本」。
        """
        if "skills" in (run.checkpoint or {}):
            return
        # P9a：独立库任务无起源课题 → 无项目作用域技能，快照留空。
        snapshot = (
            await skills_service.snapshot_for_project(session, run.project_id)
            if run.project_id is not None
            else {}
        )
        checkpoint = dict(run.checkpoint or {})
        checkpoint["skills"] = snapshot
        run.checkpoint = checkpoint
        await session.commit()

    async def _plan(self, session: AsyncSession, run: VoyageRun) -> None:
        await self._set_status(session, run, "planning")
        context = (run.checkpoint or {}).get("params")
        try:
            steps = await self.navigator.plan(run, context if isinstance(context, dict) else None)
        except NavigatorError as e:
            run.plan = []
            await self._emit_log(run, f"规划失败：{e}", level="error")
            await self._set_status(session, run, "failed")
            raise _ExternallyTerminated("failed") from e
        run.plan = steps
        if run.done_criteria is None:
            run.done_criteria = done_criteria_for_kind(run.kind)
        await session.commit()
        await self._emit_log(run, f"计划就绪，共 {len(steps)} 步", level="success")

    async def _ensure_step_rows(self, session: AsyncSession, run: VoyageRun) -> None:
        """为 plan 中尚无记录的步骤补建节点行（首次规划/断点恢复）。"""
        stmt = select(VoyageStep.seq).where(VoyageStep.run_id == run.id)
        existing = {seq for (seq,) in (await session.execute(stmt)).all()}
        for seq, step_def in enumerate(run.plan or []):
            if seq in existing:
                continue
            session.add(self._new_step_row(run, seq=seq, rank=seq * _RANK_GAP, step_def=step_def))
        await session.commit()

    def _new_step_row(
        self, run: VoyageRun, *, seq: int, rank: float, step_def: dict[str, Any]
    ) -> VoyageStep:
        provenance: dict[str, Any] = {"plan_iteration": run.plan_iteration}
        if step_def.get("on_failure"):
            provenance["on_failure"] = step_def["on_failure"]
        if step_def.get("wrapup"):
            provenance["wrapup"] = True
        return VoyageStep(
            run_id=run.id,
            seq=seq,
            rank=rank,
            title=str(step_def.get("title", f"step {seq}")),
            action=str(step_def.get("action", "")),
            params=step_def.get("params") or {},
            acceptance={
                "text": step_def.get("acceptance"),
                "checks": step_def.get("checks"),
            },
            requires_gate=step_def.get("requires_gate") or None,
            budget=step_def.get("budget") or None,
            provenance=provenance,
            status="pending",
        )

    async def _active_rows(self, session: AsyncSession, run: VoyageRun) -> list[VoyageStep]:
        """非 obsolete 节点，按清单序（rank, seq）。"""
        stmt = (
            select(VoyageStep)
            .where(VoyageStep.run_id == run.id, VoyageStep.status != "obsolete")
            .order_by(VoyageStep.rank, VoyageStep.seq)
        )
        return list((await session.execute(stmt)).scalars().all())

    def _max_attempts(self, run: VoyageRun, row: VoyageStep) -> int:
        budget = row.budget if isinstance(row.budget, dict) else {}
        declared = budget.get("max_attempts")
        if declared:
            return int(declared)
        # pipeline 默认不隐式重试（副作用步骤需显式声明），template/loop 默认重试 1 次
        return 1 if run.mode == "pipeline" else 2

    async def _loop(self, session: AsyncSession, run: VoyageRun) -> None:
        while True:
            # 协作式取消：每步开始前查 DB status
            if await self._current_db_status(session, run.id) == "cancelled":
                run.status = "cancelled"
                await self._emit_status(run)
                return

            rows = await self._active_rows(session, run)
            node_index, node = next(
                ((i, r) for i, r in enumerate(rows) if r.status != "passed"),
                (len(rows), None),
            )
            if run.cursor != node_index:
                run.cursor = node_index
                await session.commit()
            if node is None:
                await self._finalize(session, run)
                return

            step_def = _step_def_from_row(node, run.plan)

            # 预算耗尽：降级收尾（docs/voyage-loop.md §5.4）——收尾步骤放行把已完成
            # 工作变成产出，其余未执行步骤作废；确实无产出可收才 paused_error
            if self._budget_exceeded(run):
                finishing = self._budget_finishing_steps(run, rows)
                if node in finishing:
                    await self._emit_log(run, "预算已用尽，用已完成的结果收尾", level="budget")
                elif finishing:
                    dropped = [
                        r
                        for r in rows
                        if r.status not in ("passed", "obsolete") and r not in finishing
                    ]
                    for r in dropped:
                        r.status = "obsolete"
                    run.plan_iteration = run.plan_iteration + 1
                    self._record_plan_event(
                        run,
                        source="budget",
                        reason="预算用尽，跳过剩余步骤，用已完成的结果收尾",
                        added=0,
                        obsoleted=len(dropped),
                        trigger_step=None,
                    )
                    await self._regen_plan_snapshot(session, run)
                    await session.commit()
                    await self._emit_log(
                        run,
                        f"预算用尽，作废 {len(dropped)} 个未执行步骤，用已完成的结果收尾",
                        level="budget",
                    )
                    continue
                else:
                    await self._emit_log(
                        run, "预算超限，尚无产出可收尾，任务暂停（paused_error）", level="error"
                    )
                    await self._set_status(session, run, "paused_error")
                    return

            # 复位失败节点（直接 run() 再驱动而未经 resume 复位时）
            if node.status == "failed":
                if not await self._handle_failure(session, run, node, node_index, step_def):
                    return
                continue

            # 人在环闸门
            if step_def.get("requires_gate") and not await self._gate_cleared(
                session, run, node, node_index, step_def
            ):
                return

            await self._execute_and_verify(session, run, step_def, node)

            if node.verdict and node.verdict.get("passed"):
                # 节点可携带 plan_signal（如 experiment.analyze 的继续/收束判定）：
                # 走 kind 确定性分支表做计划编辑，不经 LLM（docs/voyage-loop.md §7）
                await self._apply_signal_edits(session, run, node)
                await self._set_status(session, run, "executing")
                continue
            if not await self._handle_failure(session, run, node, node_index, step_def):
                return

    async def _finalize(self, session: AsyncSession, run: VoyageRun) -> None:
        """所有节点走完 → voyage 级完成标准终检（docs/voyage-loop.md §5.4）。"""
        criteria = run.done_criteria if isinstance(run.done_criteria, dict) else None
        checks = criteria.get("checks") if criteria else None
        if checks:
            verdict, _rubrics = run_deterministic_checks(
                checks, observation=None, checkpoint=run.checkpoint
            )
            if verdict is not None and not verdict.get("passed"):
                await self._emit_log(run, f"完成标准未达成：{verdict.get('reason')}", level="error")
                # 终检回灌（loop 模式）留待后续：先一律暂停等人工，防"过早宣告完成"
                await self._set_status(session, run, "paused_error")
                return
        await self._set_status(session, run, "done")

    # ---- 闸门 ----

    async def _gate_cleared(
        self,
        session: AsyncSession,
        run: VoyageRun,
        node: VoyageStep,
        node_index: int,
        step_def: dict[str, Any],
    ) -> bool:
        """闸门已批准返回 True；否则（创建/等待/驳回）处理状态并返回 False。"""
        checkpoint = dict(run.checkpoint or {})
        gates: dict[str, Any] = dict(checkpoint.get("gates") or {})
        # 键 = 节点 id；旧数据（迁移前 in-flight run）按游标键控，做读取回退
        entry = gates.get(str(node.id)) or gates.get(str(node_index))

        if entry:
            gate = await session.get(Gate, uuid.UUID(entry["gate_id"]))
            if gate is not None and gate.status == "approved":
                await self._set_status(session, run, "executing")
                return True
            if gate is not None and gate.status == "rejected":
                await self._emit_log(run, f"审批被驳回：{gate.comment or ''}", level="error")
                await self._set_status(session, run, "failed")
                return False
            # 仍在等待审批
            await self._set_status(session, run, "paused_gate")
            return False

        payload: dict[str, Any] = {
            "voyage_id": str(run.id),
            "step_seq": node_index,
            "step_title": step_def.get("title"),
        }
        # 动作可在 checkpoint["gate_payload"] 预置业务上下文（如实验 id + 预算摘要）
        extra_payload = checkpoint.get("gate_payload")
        if isinstance(extra_payload, dict):
            payload = extra_payload | payload
        gate = Gate(
            project_id=run.project_id,
            kind=str(step_def["requires_gate"]),
            payload=payload,
            requested_by=f"voyage:{run.kind}",
        )
        session.add(gate)
        await session.flush()
        gates[str(node.id)] = {"gate_id": str(gate.id)}
        checkpoint["gates"] = gates
        run.checkpoint = checkpoint
        await session.commit()
        await self._emit_log(
            run, f"步骤 {node_index} 需要 {gate.kind} 人工审批，任务暂停", level="gate"
        )
        await self._emit_notify(
            run.project_id,
            {
                "type": "gate.created",
                "gate": {
                    "id": str(gate.id),
                    "project_id": str(gate.project_id),
                    "kind": gate.kind,
                    "status": gate.status,
                    "payload": gate.payload,
                    "requested_by": gate.requested_by,
                    "created_at": gate.created_at.isoformat() if gate.created_at else None,
                },
            },
        )
        await self._set_status(session, run, "paused_gate")
        return False

    # ---- 执行 + 验证 ----

    async def _execute_and_verify(
        self,
        session: AsyncSession,
        run: VoyageRun,
        step_def: dict[str, Any],
        step_row: VoyageStep,
    ) -> None:
        step_row.status = "running"
        step_row.attempt = step_row.attempt + 1
        step_row.started_at = utcnow()
        await session.commit()
        await self._emit_step(run, step_row)
        retry_note = f"（第 {step_row.attempt} 次尝试）" if step_row.attempt > 1 else ""
        await self._emit_log(
            run,
            f"▶ 执行 第 {run.cursor + 1} 步：{step_row.title}{retry_note}",
            level="step",
            step_id=step_row.id,
        )

        ctx = ActionContext(
            run=run,
            llm=self._llm,
            checkpoint=dict(run.checkpoint or {}),
            bus=self._bus,
            step_id=step_row.id,
        )
        observation = await self.helm.execute(ctx, step_def)
        run.checkpoint = dict(ctx.checkpoint)
        step_row.observation = observation
        step_row.finished_at = utcnow()
        action_usage = observation.get("usage") if isinstance(observation, dict) else None
        await session.commit()
        await self._emit_step(run, step_row)

        await self._set_status(session, run, "verifying")
        verdict, verify_usage = await self.sextant.verify(run, step_def, observation)
        step_row.verdict = verdict
        step_row.status = "passed" if verdict.get("passed") else "failed"
        passed = bool(verdict.get("passed"))
        reason = str(verdict.get("reason") or "")
        await self._emit_log(
            run,
            f"{'✓ 通过' if passed else '✗ 未通过'}：{step_row.title}"
            + (f" — {reason}" if reason else ""),
            level="success" if passed else "error",
            step_id=step_row.id,
        )
        step_row.tokens = self._sum_usage(action_usage or {}, verify_usage)
        self._archive_attempt(step_row)
        self._accumulate_usage(run, step_row.tokens)
        # observation 未携带 usage 的动作（如 wiki 批处理）绕过了上面的累计，
        # 以 LLMUsage 明细（router 记账，含 voyage_id）为准刷新 run.usage
        await self._refresh_usage_from_ledger(session, run)
        await session.commit()
        await self._emit_step(run, step_row)

    @staticmethod
    def _archive_attempt(step_row: VoyageStep) -> None:
        """每次尝试完整归档（docs/voyage-loop.md §4：审计留痕一律落库）。"""
        archive = list(step_row.attempts or [])
        archive.append(
            {
                "attempt": step_row.attempt,
                "observation": step_row.observation,
                "verdict": step_row.verdict,
                "tokens": step_row.tokens,
                "started_at": step_row.started_at.isoformat() if step_row.started_at else None,
                "finished_at": step_row.finished_at.isoformat() if step_row.finished_at else None,
            }
        )
        step_row.attempts = archive

    @staticmethod
    def _sum_usage(*usages: dict[str, Any]) -> dict[str, int]:
        total = {"prompt_tokens": 0, "completion_tokens": 0}
        for usage in usages:
            total["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
            total["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
        return total

    @staticmethod
    async def _refresh_usage_from_ledger(session: AsyncSession, run: VoyageRun) -> None:
        row = (
            await session.execute(
                select(
                    func.coalesce(func.sum(LLMUsage.prompt_tokens), 0),
                    func.coalesce(func.sum(LLMUsage.completion_tokens), 0),
                ).where(LLMUsage.voyage_id == run.id)
            )
        ).one()
        prompt, completion = int(row[0]), int(row[1])
        current = run.usage or {}
        # 取两者较大值：明细表可能缺少未走 router 的估算，累计值可能缺少批处理动作
        prompt = max(prompt, int(current.get("prompt_tokens", 0)))
        completion = max(completion, int(current.get("completion_tokens", 0)))
        run.usage = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }

    @staticmethod
    def _accumulate_usage(run: VoyageRun, tokens: dict[str, int]) -> None:
        usage = dict(run.usage or {})
        prompt = int(usage.get("prompt_tokens", 0)) + tokens["prompt_tokens"]
        completion = int(usage.get("completion_tokens", 0)) + tokens["completion_tokens"]
        run.usage = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }

    @staticmethod
    def _budget_exceeded(run: VoyageRun) -> bool:
        budget = run.budget or {}
        max_tokens = budget.get("max_tokens")
        if not max_tokens:
            return False
        return int((run.usage or {}).get("total_tokens", 0)) >= int(max_tokens)

    @staticmethod
    def _budget_finishing_steps(run: VoyageRun, rows: list[VoyageStep]) -> list[VoyageStep]:
        """预算耗尽时应放行的收尾步骤（docs/voyage-loop.md §5.4）：
        1. 显式 `wrapup=True` 的待办步骤（summarize / report / 终编译）；
        2. 都没有、但已有步骤完成过 → 最后一个待办步骤作**隐式收尾**（跑完它才有产出，
           兼容 wrapup 标记出现前创建的旧 run：如 debate 已跑完、summarize 无标记）；
        3. 什么都没完成 → 空（真暂停，不伪造产出）。
        """
        pending = [r for r in rows if r.status not in ("passed", "obsolete")]
        explicit = [r for r in pending if _is_wrapup(_step_def_from_row(r, run.plan))]
        if explicit:
            return explicit
        if pending and any(r.status == "passed" for r in rows):
            return [pending[-1]]
        return []

    # ---- 失败分派（docs/voyage-loop.md §5.1）----

    async def _handle_failure(
        self,
        session: AsyncSession,
        run: VoyageRun,
        node: VoyageStep,
        node_index: int,
        step_def: dict[str, Any],
    ) -> bool:
        """验证失败后的分派。返回 True 表示可继续循环，False 表示已停。"""
        diagnosis = str((node.verdict or {}).get("reason", ""))
        is_execution_error = bool((node.observation or {}).get("error"))

        # 执行类错误（工具/网络/代码异常）在节点尝试预算内原地重试；
        # 判断类失败（校验未过）重试无意义，直接进入分派
        if is_execution_error and node.attempt < self._max_attempts(run, node):
            params = dict(node.params or {})
            params["diagnosis"] = diagnosis[:2000]
            node.params = params
            node.status = "pending"
            await session.commit()
            await self._emit_log(
                run,
                f"步骤 {node_index} 执行出错，第 {node.attempt + 1} 次尝试（带诊断重试）",
                level="plan",
            )
            return True

        # 固定管线步骤可声明 on_failure="fail"：不重规划，直接判 voyage 失败
        if step_def.get("on_failure") == "fail":
            await self._emit_log(
                run, f"步骤失败（on_failure=fail，不重规划）：{diagnosis}", level="error"
            )
            await self._set_status(session, run, "failed")
            return False

        if run.mode == "pipeline":
            # 确定性管线不经 LLM 重规划：暂停等人工（修复代码后可从断点重试，
            # 前面步骤的成果不作废）
            await self._emit_log(run, f"步骤失败，任务暂停等待人工处理：{diagnosis}", level="error")
            await self._set_status(session, run, "paused_error")
            return False

        if run.mode == "template":
            # 模板骨架：确定性重规划分支表（idea_proposal），LLM 尾部替换兜底
            return await self._replan(session, run, node, step_def)
        # loop：失败回灌 Navigator 做计划编辑（阶段 D）
        return await self._navigator_edit(session, run, node, step_def)

    # ---- 计划编辑（loop 模式失败回灌 + plan_signal 分支表，docs/voyage-loop.md §5.3/§7）----

    @staticmethod
    def _plan_state_summary(rows: list[VoyageStep]) -> str:
        """给 Navigator 的计划状态摘要：尾部节点全量、更早的压缩计数（防 context rot）。"""
        lines: list[str] = []
        tail = rows[-8:]
        if len(rows) > len(tail):
            passed = sum(1 for r in rows[: len(rows) - len(tail)] if r.status == "passed")
            lines.append(f"（更早 {len(rows) - len(tail)} 步省略，其中 {passed} 步已通过）")
        for r in tail:
            reason = str((r.verdict or {}).get("reason") or "")[:120]
            suffix = f"：{reason}" if reason else ""
            lines.append(f"- id={r.id} [{r.status}] {r.title}（{r.action}）{suffix}")
        return "\n".join(lines)

    async def _regen_plan_snapshot(self, session: AsyncSession, run: VoyageRun) -> None:
        """run.plan 快照单向派生自活动节点行（节点表是唯一真源）。"""
        rows = await self._active_rows(session, run)
        run.plan = [_step_def_from_row(r, None) for r in rows]

    @staticmethod
    def _record_plan_event(
        run: VoyageRun,
        *,
        source: str,
        reason: str,
        added: int,
        obsoleted: int,
        trigger_step: str | None,
    ) -> None:
        """计划调整留痕进 checkpoint["plan_history"]（SSE log 不持久，因果叙事必须落库）。

        source: signal（执行结果规则分支）| navigator（AI 调整）| template（模板分支）。
        """
        checkpoint = dict(run.checkpoint or {})
        history = list(checkpoint.get("plan_history") or [])
        history.append(
            {
                "iteration": run.plan_iteration,
                "source": source,
                "reason": reason[:500],
                "added": added,
                "obsoleted": obsoleted,
                "trigger_step": trigger_step,
                "at": utcnow().isoformat(),
            }
        )
        checkpoint["plan_history"] = history
        run.checkpoint = checkpoint

    async def _apply_signal_edits(
        self, session: AsyncSession, run: VoyageRun, node: VoyageStep
    ) -> None:
        """节点通过后消费 observation.plan_signal：kind 确定性分支表 → 计划编辑。

        能写成规则的决策不问 LLM（docs/voyage-loop.md §5.3）；分支表自带幂等
        （待办节点已存在则返回 None，防 resume 重放导致重复追加）。
        """
        signal = (node.observation or {}).get("plan_signal")
        builder = SIGNAL_TABLES.get(run.kind)
        if not isinstance(signal, dict) or builder is None:
            return
        rows = await self._active_rows(session, run)
        edit = builder(signal, rows)
        if not edit or edit.get("finish") or not edit.get("edits"):
            return
        added = await self._apply_plan_edit(session, run, edit, anchor=node)
        run.plan_iteration = run.plan_iteration + 1
        self._record_plan_event(
            run,
            source="signal",
            reason=str(edit.get("reason") or ""),
            added=added,
            obsoleted=0,
            trigger_step=node.title,
        )
        await self._regen_plan_snapshot(session, run)
        await session.commit()
        await self._emit_log(
            run,
            f"计划已按执行结果调整：{edit.get('reason') or ''}（新增 {added} 步）",
            level="plan",
        )

    async def _apply_plan_edit(
        self,
        session: AsyncSession,
        run: VoyageRun,
        edit: dict[str, Any],
        *,
        anchor: VoyageStep | None,
    ) -> int:
        """应用一次已通过 schema 校验的计划编辑；引用非法抛 PlanEditError。

        应用期不变量（docs/voyage-loop.md §5.3）：只能编辑/作废非终态节点，
        插入位置必须在当前执行点之后；seq 只增不改，rank 取间隙值。
        返回新增节点数；调用方负责 commit 与快照重生成。
        """
        rows = await self._active_rows(session, run)
        by_id = {str(r.id): r for r in rows}
        max_passed_rank = max((r.rank for r in rows if r.status == "passed"), default=-1.0)
        max_seq = (
            await session.execute(
                select(func.coalesce(func.max(VoyageStep.seq), -1)).where(
                    VoyageStep.run_id == run.id
                )
            )
        ).scalar_one()
        next_seq = int(max_seq) + 1
        added = 0
        for op in edit["edits"]:
            if op["op"] == "add_nodes":
                if op.get("insert_after"):
                    anchor_row = by_id.get(str(op["insert_after"]))
                    if anchor_row is None:
                        raise PlanEditError(f"insert_after 引用不存在的步骤：{op['insert_after']}")
                    if anchor_row.rank < max_passed_rank:
                        raise PlanEditError("插入位置必须在当前执行点之后")
                    base = anchor_row.rank
                elif anchor is not None:
                    base = anchor.rank  # 缺省：失败/信号节点的位置
                else:
                    base = max((r.rank for r in rows), default=0.0)
                following = min((r.rank for r in rows if r.rank > base), default=None)
                nodes = op["nodes"]
                gap = (following - base) / (len(nodes) + 1) if following is not None else _RANK_GAP
                for i, node_def in enumerate(nodes):
                    session.add(
                        self._new_step_row(
                            run, seq=next_seq, rank=base + gap * (i + 1), step_def=node_def
                        )
                    )
                    next_seq += 1
                    added += 1
            elif op["op"] == "update_node":
                row = by_id.get(str(op["step_id"]))
                if row is None or row.status == "passed":
                    raise PlanEditError(f"update_node 只能修改未完成步骤：{op['step_id']}")
                patch = op["patch"]
                if "params" in patch:
                    row.params = dict(row.params or {}) | dict(patch["params"])
                if "title" in patch:
                    row.title = str(patch["title"])[:255]
                if "acceptance" in patch or "checks" in patch:
                    acc = dict(row.acceptance or {})
                    if "acceptance" in patch:
                        acc["text"] = patch["acceptance"]
                    if "checks" in patch:
                        acc["checks"] = patch["checks"]
                    row.acceptance = acc
            else:  # obsolete_nodes
                for step_id in op["step_ids"]:
                    row = by_id.get(str(step_id))
                    if row is None or row.status == "passed":
                        raise PlanEditError(f"obsolete_nodes 只能作废未完成步骤：{step_id}")
                    row.status = "obsolete"
        return added

    async def _navigator_edit(
        self,
        session: AsyncSession,
        run: VoyageRun,
        failed_node: VoyageStep,
        failed_step: dict[str, Any],
    ) -> bool:
        """loop 模式失败回灌：Navigator 产出计划编辑。返回 True 表示可继续循环。

        无进展硬停（docs/voyage-loop.md §5.4）：存在失败步骤而 Navigator 返回
        finish/noop → paused_error 等人工；编辑生效后失败节点自动作废并归档。
        """
        checkpoint = dict(run.checkpoint or {})
        replans = int(checkpoint.get("replans", 0))
        diagnosis = str((failed_node.verdict or {}).get("reason", ""))
        if replans >= MAX_REPLANS:
            await self._emit_log(
                run, f"计划调整已达上限（{MAX_REPLANS} 次），任务暂停等待人工处理", level="error"
            )
            await self._set_status(session, run, "paused_error")
            return False

        await self._set_status(session, run, "replanning")
        rows = await self._active_rows(session, run)
        failed_def = dict(failed_step) | {"step_id": str(failed_node.id)}
        try:
            edit = await self.navigator.on_result(
                run, failed_def, diagnosis, self._plan_state_summary(rows)
            )
        except NavigatorError as e:
            await self._emit_log(run, f"计划调整失败：{e}", level="error")
            await self._set_status(session, run, "paused_error")
            return False

        if edit.get("finish") or not edit.get("edits"):
            await self._emit_log(
                run,
                f"Navigator 未给出有效调整（{edit.get('reason') or 'noop'}），任务暂停等待人工",
                level="error",
            )
            await self._set_status(session, run, "paused_error")
            return False

        try:
            added = await self._apply_plan_edit(session, run, edit, anchor=failed_node)
        except PlanEditError as e:
            await self._emit_log(run, f"计划编辑被拒绝：{e}", level="error")
            await self._set_status(session, run, "paused_error")
            return False

        # 失败节点归档（保留失败态）后自动作废——Navigator 只需给出替代/补充步骤
        replaced = list(checkpoint.get("replaced_steps") or [])
        replaced.append(_serialize_step(failed_node))
        failed_node.status = "obsolete"
        checkpoint["replaced_steps"] = replaced
        checkpoint["replans"] = replans + 1
        run.checkpoint = checkpoint
        run.plan_iteration = run.plan_iteration + 1
        obsoleted = 1 + sum(
            len(op.get("step_ids") or []) for op in edit["edits"] if op["op"] == "obsolete_nodes"
        )
        self._record_plan_event(
            run,
            source="navigator",
            reason=str(edit.get("reason") or "") or diagnosis,
            added=added,
            obsoleted=obsoleted,
            trigger_step=failed_node.title,
        )
        await self._regen_plan_snapshot(session, run)
        await session.commit()
        await self._emit_log(
            run,
            f"第 {replans + 1} 次计划调整完成（{edit.get('reason') or ''}，"
            f"新增 {added} 步，诊断：{diagnosis}）",
            level="plan",
        )
        await self._set_status(session, run, "executing")
        return True

    # ---- 重规划（template 模式：确定性分支表 + LLM 尾部替换兜底）----

    async def _replan(
        self,
        session: AsyncSession,
        run: VoyageRun,
        failed_node: VoyageStep,
        failed_step: dict[str, Any],
    ) -> bool:
        """验证失败后重规划（template 模式）。返回 True 表示可继续循环，False 表示已停。

        旧尾部节点标 obsolete 留痕（不删行），新节点按 rank 间隙追加，
        run.plan 快照由节点行重新派生。
        """
        checkpoint = dict(run.checkpoint or {})
        replans = int(checkpoint.get("replans", 0))
        diagnosis = str((failed_node.verdict or {}).get("reason", ""))
        if replans >= MAX_REPLANS:
            await self._emit_log(
                run, f"重规划已达上限（{MAX_REPLANS} 次），任务暂停等待人工处理", level="error"
            )
            await self._set_status(session, run, "paused_error")
            return False

        await self._set_status(session, run, "replanning")
        try:
            new_tail = await self.navigator.replan(run, failed_step, diagnosis)
        except NavigatorError as e:
            await self._emit_log(run, f"重规划失败：{e}", level="error")
            await self._set_status(session, run, "paused_error")
            return False

        # 失败节点起的旧尾部整体作废（留痕；失败节点本身归档进 checkpoint 兼容回放）
        rows = await self._active_rows(session, run)
        tail = [r for r in rows if (r.rank, r.seq) >= (failed_node.rank, failed_node.seq)]
        for row in tail:
            row.status = "obsolete"
        replaced = list(checkpoint.get("replaced_steps") or [])
        replaced.append(_serialize_step(failed_node))
        checkpoint["replaced_steps"] = replaced
        checkpoint["replans"] = replans + 1
        run.checkpoint = checkpoint
        run.plan_iteration = run.plan_iteration + 1

        # 新节点追加：seq 只增不改，rank 从失败节点位置继续
        max_seq = (
            await session.execute(
                select(func.coalesce(func.max(VoyageStep.seq), -1)).where(
                    VoyageStep.run_id == run.id
                )
            )
        ).scalar_one()
        for i, step_def in enumerate(new_tail):
            session.add(
                self._new_step_row(
                    run,
                    seq=int(max_seq) + 1 + i,
                    rank=failed_node.rank + i * _RANK_GAP,
                    step_def=step_def,
                )
            )
        self._record_plan_event(
            run,
            source="template",
            reason=diagnosis or "步骤未通过，按模板分支重排剩余步骤",
            added=len(new_tail),
            obsoleted=len(tail),
            trigger_step=failed_node.title,
        )
        # run.plan 快照单向派生：已通过前缀 + 新尾部
        passed_defs = [_step_def_from_row(r, run.plan) for r in rows if r.status == "passed"]
        run.plan = passed_defs + list(new_tail)
        await session.commit()
        await self._emit_log(
            run,
            f"第 {replans + 1} 次重规划完成，剩余 {len(new_tail)} 步（诊断：{diagnosis}）",
            level="plan",
        )
        await self._set_status(session, run, "executing")
        return True
