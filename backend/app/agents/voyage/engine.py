"""VoyageEngine：持久化状态机，驱动 Navigator/Helm/Sextant 三元组闭环。

状态机（docs/architecture.md §3）：
    planning → executing → verifying ─┬→ (下一步) executing
                                      ├→ replanning → executing
                                      ├→ paused_gate（审批后 resume）
                                      ├→ paused_error（连续重规划超限）
                                      └→ done / failed
要点：
- 每步执行/判定后持久化 cursor + checkpoint，worker 崩溃后可从断点续跑；
- requires_gate 步骤执行前创建 Gate 并暂停（结束本次 ARQ 任务），approve 后
  由 resume_voyage 续跑；
- cancel 协作式：每步开始前查 DB status，状态写入用条件 UPDATE 防覆盖 cancelled；
- 全程向 Redis ``voyage:{id}:events`` 发布 status/step/log 事件；
- 步骤与 Sextant 的 tokens 累加到 run.usage，超出 budget.max_tokens 则暂停。
"""

import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.voyage.actions import ActionContext
from app.agents.voyage.helm import Helm
from app.agents.voyage.navigator import Navigator, NavigatorError
from app.agents.voyage.sextant import Sextant
from app.core.db import get_sessionmaker
from app.core.events import EventBus
from app.core.llm.router import LLMRouter, get_llm_router
from app.models.base import utcnow
from app.models.gate import Gate
from app.models.llm_config import LLMUsage
from app.models.voyage import TERMINAL_STATUSES, VoyageRun, VoyageStep

MAX_REPLANS = 2


class _ExternallyTerminated(Exception):
    """状态被外部置为终态（如用户 cancel），本次驱动直接退出。"""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


def _serialize_step(step: VoyageStep) -> dict[str, Any]:
    return {
        "id": str(step.id),
        "seq": step.seq,
        "title": step.title,
        "action": step.action,
        "params": step.params,
        "observation": step.observation,
        "verdict": step.verdict,
        "status": step.status,
        "tokens": step.tokens,
        "started_at": step.started_at.isoformat() if step.started_at else None,
        "finished_at": step.finished_at.isoformat() if step.finished_at else None,
    }


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
        self._llm = llm
        self.navigator = Navigator(llm)
        self.helm = Helm()
        self.sextant = Sextant(llm)

    # ---- 入口 ----

    async def run(self, run_id: uuid.UUID) -> None:
        """首次驱动：无 plan 则先规划，再进入执行循环。"""
        await self._drive(run_id)

    async def resume(self, run_id: uuid.UUID) -> None:
        """闸门审批 / worker 重启后从 cursor 断点续跑。"""
        await self._drive(run_id)

    # ---- 事件发布 ----

    async def _emit_voyage(self, run_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
        if self._bus is not None:
            await self._bus.publish_voyage_event(run_id, event, data)

    async def _emit_notify(self, project_id: uuid.UUID, message: dict[str, Any]) -> None:
        if self._bus is not None:
            await self._bus.publish_notify(project_id, message)

    async def _emit_status(self, run: VoyageRun) -> None:
        await self._emit_voyage(run.id, "status", {"status": run.status, "cursor": run.cursor})
        await self._emit_notify(
            run.project_id,
            {"type": "voyage.status", "voyage_id": str(run.id), "status": run.status},
        )

    async def _emit_step(self, run: VoyageRun, step: VoyageStep) -> None:
        await self._emit_voyage(run.id, "step", {"step": _serialize_step(step)})

    async def _emit_log(self, run: VoyageRun, message: str) -> None:
        await self._emit_voyage(run.id, "log", {"message": message})

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
                if run.plan is None:
                    await self._plan(session, run)
                await self._ensure_step_rows(session, run)
                if run.status == "planning":
                    await self._set_status(session, run, "executing")
                await self._loop(session, run)
            except _ExternallyTerminated:
                # 外部 cancel：补发一次终态事件后安静退出
                await self._emit_status(run)

    async def _plan(self, session: AsyncSession, run: VoyageRun) -> None:
        await self._set_status(session, run, "planning")
        context = (run.checkpoint or {}).get("params")
        try:
            steps = await self.navigator.plan(run, context if isinstance(context, dict) else None)
        except NavigatorError as e:
            run.plan = []
            await self._emit_log(run, f"规划失败：{e}")
            await self._set_status(session, run, "failed")
            raise _ExternallyTerminated("failed") from e
        run.plan = steps
        await session.commit()
        await self._emit_log(run, f"计划就绪，共 {len(steps)} 步")

    async def _ensure_step_rows(self, session: AsyncSession, run: VoyageRun) -> None:
        """为 plan 中尚无记录的步骤补建 VoyageStep 行（断点恢复/重规划后）。"""
        stmt = select(VoyageStep.seq).where(VoyageStep.run_id == run.id)
        existing = {seq for (seq,) in (await session.execute(stmt)).all()}
        for seq, step_def in enumerate(run.plan or []):
            if seq in existing:
                continue
            session.add(
                VoyageStep(
                    run_id=run.id,
                    seq=seq,
                    title=str(step_def.get("title", f"step {seq}")),
                    action=str(step_def.get("action", "")),
                    params=step_def.get("params") or {},
                    status="pending",
                )
            )
        await session.commit()

    async def _get_step_row(self, session: AsyncSession, run: VoyageRun, seq: int) -> VoyageStep:
        stmt = select(VoyageStep).where(VoyageStep.run_id == run.id, VoyageStep.seq == seq)
        return (await session.execute(stmt)).scalar_one()

    async def _loop(self, session: AsyncSession, run: VoyageRun) -> None:
        while True:
            # 协作式取消：每步开始前查 DB status
            if await self._current_db_status(session, run.id) == "cancelled":
                run.status = "cancelled"
                await self._emit_status(run)
                return

            plan: list[dict[str, Any]] = list(run.plan or [])
            if run.cursor >= len(plan):
                await self._set_status(session, run, "done")
                return
            step_def = plan[run.cursor]

            # 预算：超限自动暂停
            if self._budget_exceeded(run):
                await self._emit_log(run, "预算超限，航程暂停（paused_error）")
                await self._set_status(session, run, "paused_error")
                return

            # 人在环闸门
            if step_def.get("requires_gate") and not await self._gate_cleared(
                session, run, step_def
            ):
                return

            step_row = await self._get_step_row(session, run, run.cursor)
            await self._execute_and_verify(session, run, step_def, step_row)

            if step_row.verdict and step_row.verdict.get("passed"):
                run.cursor += 1
                await session.commit()
                if run.cursor >= len(run.plan or []):
                    await self._set_status(session, run, "done")
                    return
                await self._set_status(session, run, "executing")
            else:
                if not await self._replan(session, run, step_def, step_row):
                    return

    # ---- 闸门 ----

    async def _gate_cleared(
        self, session: AsyncSession, run: VoyageRun, step_def: dict[str, Any]
    ) -> bool:
        """闸门已批准返回 True；否则（创建/等待/驳回）处理状态并返回 False。"""
        checkpoint = dict(run.checkpoint or {})
        gates: dict[str, Any] = dict(checkpoint.get("gates") or {})
        entry = gates.get(str(run.cursor))

        if entry:
            gate = await session.get(Gate, uuid.UUID(entry["gate_id"]))
            if gate is not None and gate.status == "approved":
                await self._set_status(session, run, "executing")
                return True
            if gate is not None and gate.status == "rejected":
                await self._emit_log(run, f"闸门被驳回：{gate.comment or ''}")
                await self._set_status(session, run, "failed")
                return False
            # 仍在等待审批
            await self._set_status(session, run, "paused_gate")
            return False

        gate = Gate(
            project_id=run.project_id,
            kind=str(step_def["requires_gate"]),
            payload={
                "voyage_id": str(run.id),
                "step_seq": run.cursor,
                "step_title": step_def.get("title"),
            },
            requested_by=f"voyage:{run.kind}",
        )
        session.add(gate)
        await session.flush()
        gates[str(run.cursor)] = {"gate_id": str(gate.id)}
        checkpoint["gates"] = gates
        run.checkpoint = checkpoint
        await session.commit()
        await self._emit_log(run, f"步骤 {run.cursor} 需要 {gate.kind} 闸门审批，航程暂停")
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
        step_row.started_at = utcnow()
        await session.commit()
        await self._emit_step(run, step_row)

        ctx = ActionContext(
            run=run, llm=self._llm, checkpoint=dict(run.checkpoint or {}), bus=self._bus
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
        step_row.tokens = self._sum_usage(action_usage or {}, verify_usage)
        self._accumulate_usage(run, step_row.tokens)
        # observation 未携带 usage 的动作（如 wiki 批处理）绕过了上面的累计，
        # 以 LLMUsage 明细（router 记账，含 voyage_id）为准刷新 run.usage
        await self._refresh_usage_from_ledger(session, run)
        await session.commit()
        await self._emit_step(run, step_row)

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

    # ---- 重规划 ----

    async def _replan(
        self,
        session: AsyncSession,
        run: VoyageRun,
        failed_step: dict[str, Any],
        step_row: VoyageStep,
    ) -> bool:
        """验证失败后重规划。返回 True 表示可继续循环，False 表示已停。"""
        checkpoint = dict(run.checkpoint or {})
        replans = int(checkpoint.get("replans", 0))
        diagnosis = (step_row.verdict or {}).get("reason", "")
        if replans >= MAX_REPLANS:
            await self._emit_log(run, f"重规划已达上限（{MAX_REPLANS} 次），航程暂停等待人工处理")
            await self._set_status(session, run, "paused_error")
            return False

        await self._set_status(session, run, "replanning")
        try:
            new_tail = await self.navigator.replan(run, failed_step, str(diagnosis))
        except NavigatorError as e:
            await self._emit_log(run, f"重规划失败：{e}")
            await self._set_status(session, run, "paused_error")
            return False

        plan = list(run.plan or [])
        run.plan = plan[: run.cursor] + new_tail
        checkpoint["replans"] = replans + 1
        # 被替换的失败步骤归档进 checkpoint 留痕（行记录将被新计划覆盖）
        replaced = list(checkpoint.get("replaced_steps") or [])
        replaced.append(_serialize_step(step_row))
        checkpoint["replaced_steps"] = replaced
        run.checkpoint = checkpoint
        # 丢弃失败步骤起的旧步骤记录，为新计划补建
        await session.execute(
            delete(VoyageStep).where(VoyageStep.run_id == run.id, VoyageStep.seq >= run.cursor)
        )
        await session.commit()
        await self._ensure_step_rows(session, run)
        await self._emit_log(
            run, f"第 {replans + 1} 次重规划完成，剩余 {len(new_tail)} 步（诊断：{diagnosis}）"
        )
        await self._set_status(session, run, "executing")
        return True
