"""调度器 — 序号队列 + 通道锁

架构评审的核心结论：
    序号 seq 给出「先后」，通道锁给出「能否并行」。两者缺一不可。

执行语义：
    - 按 seq 升序遍历
    - step.parallel 且与已运行步骤通道不冲突 → 并发启动
    - 否则等待所依赖通道释放后启动
    - 机械失败（OSC 丢包 / TTS 超时）本地重试 1 次
    - 语义失败（动作不存在 / 目标丢失）→ 终止后续步骤，触发立即心跳
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from ..contracts.facts import ActivePlan
from ..contracts.plan import (
    ActionKind, Channel, Plan, PlanResult, PlanStep, StepResult, StepStatus,
    estimated_plan_seconds, estimated_step_seconds,
)
from .state_store import StateStore

logger = logging.getLogger(__name__)

#: 执行器签名：async (step) -> 可选的结果描述；抛异常即失败
Executor = Callable[[PlanStep], Awaitable[Optional[str]]]
IOAllowanceFn = Callable[[Plan], float]

#: 单步默认超时（秒）
DEFAULT_STEP_TIMEOUT = 12.0


class SemanticError(RuntimeError):
    """语义失败 —— 不重试，直接终止计划"""


class MechanicalError(RuntimeError):
    """机械失败 —— 本地重试 1 次"""


class Scheduler:
    """计划执行引擎

    调度器自身不知道任何执行细节；所有 ActionKind 由外部注册执行器。
    这让 avatar / speech / posture / locomotion 各服务可独立替换。
    """

    def __init__(
        self,
        state: StateStore,
        on_plan_done=None,
        max_plan_seconds: float = 5.0,
        *,
        io_allowance_fn: Optional[IOAllowanceFn] = None,
        budget_exempt_actions: frozenset[ActionKind] = frozenset({ActionKind.APPROACH}),
    ) -> None:
        self._state = state
        self._max_plan_seconds = float(max_plan_seconds)
        self._io_allowance_fn = io_allowance_fn
        self._budget_exempt_actions = budget_exempt_actions
        self._executors: dict[ActionKind, Executor] = {}
        self._channel_locks: dict[Channel, asyncio.Lock] = {
            ch: asyncio.Lock() for ch in Channel
        }
        self._current: Optional[asyncio.Task] = None
        self._current_plan_id: Optional[str] = None
        self._run_lock = asyncio.Lock()
        self._on_plan_done = on_plan_done

    def _is_budget_exempt(self, plan: Plan) -> bool:
        return bool(plan.steps) and all(
            step.action in self._budget_exempt_actions for step in plan.steps
        )

    # ── 注册 ───────────────────────────────────────────────

    def register(self, kind: ActionKind, executor: Executor) -> None:
        self._executors[kind] = executor
        logger.debug("[Sched] 注册执行器 %s", kind.value)

    def registered_kinds(self) -> frozenset[ActionKind]:
        return frozenset(self._executors)

    # ── 执行 ───────────────────────────────────────────────

    async def submit(self, plan: Plan) -> PlanResult:
        """提交计划并等待完成。同一时刻只跑一个计划。"""
        try:
            estimated = estimated_plan_seconds(plan)
        except ValueError as exc:
            return PlanResult(plan.plan_id, StepStatus.FAILED, detail=f"计划时间参数无效: {exc}")
        exempt = self._is_budget_exempt(plan)
        if not exempt and estimated > self._max_plan_seconds:
            return PlanResult(
                plan.plan_id, StepStatus.FAILED,
                detail=f"计划预计 {estimated:.2f}s，超过 {self._max_plan_seconds:.2f}s 预算",
            )
        async with self._run_lock:
            if self._current is not None and not self._current.done():
                logger.warning("[Sched] 已有计划在执行，取消旧计划 %s", self._current_plan_id)
                self._current.cancel()
                await asyncio.gather(self._current, return_exceptions=True)
            task = asyncio.create_task(self._run_plan(plan), name=f"plan:{plan.plan_id}")
            self._current = task
            self._current_plan_id = plan.plan_id

        base_deadline = estimated if exempt else self._max_plan_seconds
        io_allowance = (
            max(0.0, float(self._io_allowance_fn(plan)))
            if self._io_allowance_fn is not None
            else 0.0
        )
        deadline_s = base_deadline + io_allowance

        try:
            async with asyncio.timeout(deadline_s):
                return await task
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self._state.release_all_channels()
            result = PlanResult(
                plan.plan_id, StepStatus.FAILED,
                detail=f"计划执行超过 {deadline_s:.2f}s 墙钟期限",
            )
            await self._state.record_result(result.summary())
            if self._on_plan_done is not None:
                try:
                    await self._on_plan_done(result)
                except Exception:
                    logger.exception("[Sched] 计划超时回调失败")
            return result
        except asyncio.CancelledError:
            return PlanResult(plan.plan_id, StepStatus.CANCELLED, detail="被新计划取代")
        finally:
            if self._current is task:
                self._current = None
                self._current_plan_id = None

    async def cancel_current(self, reason: str = "cancelled") -> None:
        task = self._current
        if task is not None and not task.done():
            logger.info("[Sched] 取消当前计划：%s", reason)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._state.release_all_channels()

    @property
    def busy(self) -> bool:
        return self._current is not None and not self._current.done()

    # ── 内部执行流程 ───────────────────────────────────────

    async def _run_plan(self, plan: Plan) -> PlanResult:
        total = len(plan.steps)
        await self._state.set_active_plan(ActivePlan(plan.plan_id, 0, total))
        logger.info("[Sched] 开始执行 %s（%d 步）", plan.plan_id, total)

        results: list[StepResult] = []
        running: list[tuple[asyncio.Task, PlanStep, frozenset[Channel]]] = []
        status = StepStatus.SUCCESS
        detail = ""

        try:
            for step in plan.steps:
                # 非并行步骤必须等待前面所有并发步骤结束
                if not step.parallel and running:
                    results.extend(await self._drain(running))
                    running.clear()
                    if _has_failure(results):
                        break

                # 并行步骤若与在跑的步骤通道冲突，同样必须等待
                if step.parallel and running:
                    occupied = frozenset().union(*(ch for _, _, ch in running))
                    if occupied & step.channels():
                        results.extend(await self._drain(running))
                        running.clear()
                        if _has_failure(results):
                            break

                await self._state.advance_plan(step.seq)
                channels = step.channels()
                task = asyncio.create_task(
                    self._run_step(plan.plan_id, step, channels),
                    name=f"{plan.plan_id}.s{step.seq}",
                )
                running.append((task, step, channels))

            if running and not _has_failure(results):
                results.extend(await self._drain(running))
                running.clear()

        except asyncio.CancelledError:
            for task, _, _ in running:
                task.cancel()
            await asyncio.gather(*(t for t, _, _ in running), return_exceptions=True)
            await self._state.release_all_channels()
            raise
        finally:
            for task, _, _ in running:
                if not task.done():
                    task.cancel()

        # 汇总
        failed = [r for r in results if r.status is StepStatus.FAILED]
        if failed:
            status = StepStatus.FAILED
            detail = failed[0].detail
            # 未执行的步骤标记为跳过
            done_seqs = {r.seq for r in results}
            for step in plan.steps:
                if step.seq not in done_seqs:
                    results.append(
                        StepResult(step.seq, step.action, StepStatus.SKIPPED, "前序步骤失败")
                    )

        result = PlanResult(
            plan_id=plan.plan_id,
            status=status,
            steps=tuple(sorted(results, key=lambda r: r.seq)),
            detail=detail,
        )
        await self._state.record_result(result.summary())
        logger.info("[Sched] %s", result.summary())

        if self._on_plan_done is not None:
            try:
                await self._on_plan_done(result)
            except Exception:
                logger.exception("[Sched] 计划完成回调失败")
        return result

    async def _drain(self, running) -> list[StepResult]:
        gathered = await asyncio.gather(
            *(task for task, _, _ in running), return_exceptions=True
        )
        out: list[StepResult] = []
        for outcome, (_, step, _) in zip(gathered, running):
            if isinstance(outcome, StepResult):
                out.append(outcome)
            elif isinstance(outcome, asyncio.CancelledError):
                out.append(StepResult(step.seq, step.action, StepStatus.CANCELLED, "被取消"))
            elif isinstance(outcome, BaseException):
                out.append(
                    StepResult(step.seq, step.action, StepStatus.FAILED, str(outcome))
                )
        return out

    async def _run_step(
        self, plan_id: str, step: PlanStep, channels: frozenset[Channel]
    ) -> StepResult:
        """获取通道锁 → 执行 → 释放。锁按枚举顺序获取以避免死锁。"""
        executor = self._executors.get(step.action)
        if executor is None:
            return StepResult(
                step.seq, step.action, StepStatus.FAILED,
                f"无执行器: {step.action.value}",
            )

        holder = f"{plan_id}.s{step.seq}"
        ordered = sorted(channels, key=lambda c: list(Channel).index(c))
        acquired: list[Channel] = []
        started = time.monotonic()

        try:
            for ch in ordered:
                await self._channel_locks[ch].acquire()
                acquired.append(ch)

            duration = estimated_step_seconds(step)
            await self._state.acquire_channels(channels, holder, duration)

            step_allowance = (
                max(0.0, float(self._io_allowance_fn(Plan(steps=(step,), plan_id=plan_id))))
                if self._io_allowance_fn is not None and "timeout" not in step.args
                else 0.0
            )
            timeout = float(step.args.get("timeout", DEFAULT_STEP_TIMEOUT)) + step_allowance
            detail = await self._execute_with_retry(executor, step, timeout)
            elapsed = time.monotonic() - started
            logger.info("[Sched] %s %s 完成（%.2fs）", holder, step.action.value, elapsed)
            return StepResult(step.seq, step.action, StepStatus.SUCCESS, detail or "")

        except asyncio.CancelledError:
            # 必须向上传播，让计划级 deadline、新计划替换和紧急停止真正生效。
            raise
        except SemanticError as exc:
            return StepResult(step.seq, step.action, StepStatus.FAILED, str(exc))
        except Exception as exc:
            logger.warning("[Sched] %s 失败：%s", holder, exc)
            return StepResult(step.seq, step.action, StepStatus.FAILED, str(exc))
        finally:
            await self._state.release_channels(frozenset(acquired))
            for ch in reversed(acquired):
                self._channel_locks[ch].release()

    async def _execute_with_retry(
        self, executor: Executor, step: PlanStep, timeout: float
    ) -> Optional[str]:
        """机械失败重试一次；语义失败立即上抛"""
        try:
            return await asyncio.wait_for(executor(step), timeout=timeout)
        except SemanticError:
            raise
        except (MechanicalError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("[Sched] 机械失败，重试一次：%s", exc)
            return await asyncio.wait_for(executor(step), timeout=timeout)


def _has_failure(results: list[StepResult]) -> bool:
    return any(r.status is StepStatus.FAILED for r in results)
