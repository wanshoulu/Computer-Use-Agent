"""
Task Manager — Computer Use Agent 任务生命周期管理

P2: 统一任务生命周期与真实取消。

状态机:
  queued → planning → executing → waiting_user → executing (循环)
                                     ↓
                            succeeded / failed / cancelled

TaskManager 单例管理所有任务，支持:
  - create_task() — 创建任务，异步启动 _run_task()
  - cancel_task(task_id) — 设置 _cancel_event，终止执行
  - subscribe(task_id) — AsyncGenerator 从 _event_queue 读事件，20s 无事件发 ping
  - get_task() / list_tasks() — 查询
"""
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List, AsyncGenerator, Any

from src.ontomind_agent.utils.logger import logger


class TaskState(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    EXECUTING = "executing"
    WAITING_USER = "waiting_user"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ToolStep:
    """单个工具执行步骤"""
    step_id: str
    name: str
    params: Dict[str, Any] = field(default_factory=dict)
    result: Optional[str] = None
    screenshot_before: Optional[str] = None
    screenshot_after: Optional[str] = None
    duration_ms: Optional[int] = None
    status: str = "pending"  # pending / running / success / error
    error: Optional[str] = None


@dataclass
class Task:
    """Computer Use Agent 任务"""
    task_id: str
    message: str
    model: str
    session_id: str
    state: TaskState = TaskState.QUEUED
    steps: List[ToolStep] = field(default_factory=list)
    conclusion: Optional[str] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    # 内部控制
    _cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _event_queue: asyncio.Queue = field(default_factory=asyncio.Queue, repr=False)
    _system_prompt: str = ""

    def to_dict(self) -> dict:
        """序列化为可 JSON 化的 dict（不含内部 Event/Queue）"""
        return {
            "task_id": self.task_id,
            "message": self.message,
            "model": self.model,
            "session_id": self.session_id,
            "state": self.state.value,
            "steps": [
                {
                    "step_id": s.step_id,
                    "name": s.name,
                    "params": s.params,
                    "result": s.result,
                    "duration_ms": s.duration_ms,
                    "status": s.status,
                    "error": s.error,
                }
                for s in self.steps
            ],
            "conclusion": self.conclusion,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def push_event(self, event: dict):
        """向事件队列推送事件"""
        self._event_queue.put_nowait(event)
        self.updated_at = datetime.now().isoformat()


class TaskManager:
    """任务管理器单例

    管理所有 Computer Use Agent 任务的创建、执行、取消和查询。
    """

    def __init__(self):
        self._tasks: Dict[str, Task] = {}
        self._lock = asyncio.Lock()

    async def create_task(
        self,
        message: str,
        model: str,
        session_id: str = "",
        system_prompt: str = "",
    ) -> Task:
        """创建新任务并异步启动执行"""
        # P4: 如果未指定 system_prompt，使用 Computer Use 默认提示词
        if not system_prompt:
            from src.ontomind_agent.services.computer_use_system import get_system_prompt
            system_prompt = get_system_prompt(message)

        task_id = f"task_{uuid.uuid4().hex[:12]}"
        task = Task(
            task_id=task_id,
            message=message,
            model=model,
            session_id=session_id or str(uuid.uuid4()),
            _system_prompt=system_prompt,
        )
        async with self._lock:
            self._tasks[task_id] = task

        # 异步启动任务执行
        asyncio.create_task(self._run_task(task))
        return task

    async def _run_task(self, task: Task):
        """任务状态机驱动：planning → executing，消费 pool.chat() 事件流"""
        try:
            task.state = TaskState.PLANNING
            task.push_event({"type": "state", "state": "planning", "task_id": task.task_id})

            # 检查取消
            if task._cancel_event.is_set():
                task.state = TaskState.CANCELLED
                task.push_event({"type": "done", "state": "cancelled", "task_id": task.task_id})
                return

            # 解析模型配置
            from src.ontomind_agent.routes.hermes import _resolve_model_config, _write_hermes_config
            model_config = await _resolve_model_config(task.model)
            if not model_config or not model_config.get("api_key"):
                task.state = TaskState.FAILED
                task.error = f"模型配置无效: {task.model}"
                task.push_event({"type": "error", "error": task.error, "task_id": task.task_id})
                task.push_event({"type": "done", "state": "failed", "task_id": task.task_id})
                return

            _write_hermes_config(model_config)

            task.state = TaskState.EXECUTING
            task.push_event({"type": "state", "state": "executing", "task_id": task.task_id})

            # 通过 HermesAgentPool 执行对话
            from src.ontomind_agent.services.hermes_pool import get_hermes_pool
            pool = get_hermes_pool()

            full_response_parts = []
            tool_call_count = 0

            async for event in pool.chat(
                model_key=task.model,
                model_config=model_config,
                message=task.message,
                session_id=task.session_id,
                ephemeral_system_prompt=task._system_prompt,
            ):
                # 检查取消
                if task._cancel_event.is_set():
                    task.state = TaskState.CANCELLED
                    task.push_event({"type": "done", "state": "cancelled", "task_id": task.task_id})
                    logger.info(f"[TaskManager] Task {task.task_id} cancelled during execution")
                    return

                event_type = event.get("type", "")

                if event_type == "tool_start":
                    tool_call_count += 1
                    # P4: 工具调用上限约束
                    from src.ontomind_agent.services.computer_use_system import MAX_TOOL_CALLS_PER_TASK
                    if tool_call_count > MAX_TOOL_CALLS_PER_TASK:
                        task.state = TaskState.FAILED
                        task.error = f"工具调用次数超限 ({MAX_TOOL_CALLS_PER_TASK})"
                        task.push_event({"type": "error", "error": task.error, "task_id": task.task_id})
                        task.push_event({"type": "done", "state": "failed", "task_id": task.task_id})
                        logger.warning(f"[TaskManager] Task {task.task_id} exceeded tool call limit")
                        return
                    step = ToolStep(
                        step_id=event.get("tool_call_id", uuid.uuid4().hex[:8]),
                        name=event.get("name", "unknown"),
                        status="running",
                    )
                    task.steps.append(step)
                    # P6: 审计日志
                    from src.ontomind_agent.services.task_policy import get_audit_logger
                    get_audit_logger().log(
                        task_id=task.task_id,
                        action="tool_start",
                        tool_name=step.name,
                        params=event.get("args"),
                    )
                    task.push_event({
                        "type": "tool_start",
                        "task_id": task.task_id,
                        "step_id": step.step_id,
                        "name": step.name,
                        "message": event.get("message", ""),
                    })

                elif event_type == "tool_end":
                    step_id = event.get("tool_call_id", "")
                    for s in reversed(task.steps):
                        if s.step_id == step_id:
                            s.status = "success" if event.get("success", True) else "error"
                            s.duration_ms = event.get("duration_ms")
                            s.error = event.get("error")
                            break
                    task.push_event({
                        "type": "tool_end",
                        "task_id": task.task_id,
                        "step_id": step_id,
                        "name": event.get("name", ""),
                        "duration_ms": event.get("duration_ms"),
                        "success": event.get("success", True),
                    })
                    # P6: 审计日志
                    get_audit_logger().log(
                        task_id=task.task_id,
                        action="tool_end",
                        tool_name=event.get("name", ""),
                        result_status="success" if event.get("success", True) else "error",
                        duration_ms=event.get("duration_ms"),
                    )

                elif event_type == "token":
                    content = event.get("content", "")
                    if content:
                        full_response_parts.append(content)
                        task.push_event({
                            "type": "content",
                            "task_id": task.task_id,
                            "content": content,
                            "status": "streaming",
                        })

                elif event_type == "thinking":
                    task.push_event({
                        "type": "thinking",
                        "task_id": task.task_id,
                        "content": event.get("content", ""),
                    })

                elif event_type == "result":
                    if event.get("status") == "ok":
                        response_text = event.get("response") or ""
                        if response_text and not full_response_parts:
                            full_response_parts.append(response_text)
                        task.conclusion = "".join(full_response_parts)
                        task.state = TaskState.SUCCEEDED
                        task.push_event({
                            "type": "content",
                            "task_id": task.task_id,
                            "content": task.conclusion,
                            "status": "response_complete",
                        })
                        task.push_event({"type": "done", "state": "succeeded", "task_id": task.task_id})
                    else:
                        task.state = TaskState.FAILED
                        task.error = event.get("error", "Unknown error")
                        task.push_event({"type": "error", "error": task.error, "task_id": task.task_id})
                        task.push_event({"type": "done", "state": "failed", "task_id": task.task_id})
                    return

                elif event_type == "error":
                    task.state = TaskState.FAILED
                    task.error = event.get("error", "Unknown error")
                    task.push_event({"type": "error", "error": task.error, "task_id": task.task_id})
                    task.push_event({"type": "done", "state": "failed", "task_id": task.task_id})
                    return

        except asyncio.CancelledError:
            task.state = TaskState.CANCELLED
            task.push_event({"type": "done", "state": "cancelled", "task_id": task.task_id})
            raise
        except Exception as e:
            logger.error(f"[TaskManager] Task {task.task_id} crashed: {e}", exc_info=True)
            task.state = TaskState.FAILED
            task.error = str(e)
            task.push_event({"type": "error", "error": str(e), "task_id": task.task_id})
            task.push_event({"type": "done", "state": "failed", "task_id": task.task_id})

    async def cancel_task(self, task_id: str) -> bool:
        """取消任务 — 设置 _cancel_event"""
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED):
            return False  # 已结束，不能取消
        task._cancel_event.set()
        task.state = TaskState.CANCELLED
        task.push_event({"type": "done", "state": "cancelled", "task_id": task.task_id})
        logger.info(f"[TaskManager] Task {task_id} cancelled by user")
        return True

    async def subscribe(self, task_id: str) -> AsyncGenerator[str, None]:
        """SSE 事件订阅 — 先发 snapshot，再推事件，20s 无事件发 ping"""
        task = self._tasks.get(task_id)
        if task is None:
            yield json.dumps({"type": "error", "error": "Task not found"}, ensure_ascii=False) + "\n"
            return

        # 先发送当前状态快照
        yield json.dumps({
            "type": "snapshot",
            "task": task.to_dict(),
        }, ensure_ascii=False) + "\n"

        # 如果任务已结束，直接发送 done
        if task.state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED):
            yield json.dumps({"type": "done", "state": task.state.value, "task_id": task_id},
                             ensure_ascii=False) + "\n"
            return

        # 持续推送事件
        while True:
            try:
                event = await asyncio.wait_for(task._event_queue.get(), timeout=20)
                yield json.dumps(event, ensure_ascii=False) + "\n"
                if event.get("type") == "done":
                    return
            except asyncio.TimeoutError:
                # 20s 无事件，发 ping 保持连接
                yield json.dumps({"type": "ping", "task_id": task_id}, ensure_ascii=False) + "\n"

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> List[Task]:
        return list(self._tasks.values())


# 全局单例
_task_manager: Optional[TaskManager] = None


def get_task_manager() -> TaskManager:
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager
