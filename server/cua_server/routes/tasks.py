"""
Browser Task API 路由 — Computer Use Agent 任务化接口

P2: 替代旧的 /hermes/chat/stream 链路，提供完整的任务生命周期管理。

端点:
  POST   /tasks                  — 创建任务（SSE 事件流）
  GET    /tasks/{task_id}        — 查询任务状态
  GET    /tasks/{task_id}/events — 订阅任务事件（SSE）
  POST   /tasks/{task_id}/cancel — 取消任务
  GET    /tasks                  — 列出所有任务
"""
import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from cua_server.services.task_manager import get_task_manager, TaskState
from cua_server.utils.logger import logger

router = APIRouter()


class CreateTaskRequest(BaseModel):
    """创建任务请求"""
    message: str
    model: str
    session_id: Optional[str] = None
    system_prompt: Optional[str] = ""


@router.post("/tasks")
async def create_task(req: CreateTaskRequest):
    """创建 Computer Use Agent 任务

    返回 SSE 事件流（text/event-stream），客户端通过 ReadableStream 消费。
    """
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message 不能为空")
    if not req.model:
        raise HTTPException(status_code=400, detail="model 不能为空")

    tm = get_task_manager()
    task = await tm.create_task(
        message=req.message,
        model=req.model,
        session_id=req.session_id or "",
        system_prompt=req.system_prompt or "",
    )

    logger.info(f"[BrowserTask] Task created: {task.task_id}, model={req.model}")

    # 直接返回 SSE 流（创建任务的同时开始订阅）
    async def event_stream():
        try:
            async for chunk in tm.subscribe(task.task_id):
                yield chunk
        except Exception as e:
            logger.error(f"[BrowserTask] SSE stream error for {task.task_id}: {e}")
            yield json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False) + "\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx 不缓冲
        },
    )


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    """查询任务状态"""
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return task.to_dict()


@router.get("/tasks/{task_id}/events")
async def subscribe_task_events(task_id: str):
    """订阅任务事件 SSE 流"""
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

    async def event_stream():
        async for chunk in tm.subscribe(task_id):
            yield chunk

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    """取消任务"""
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

    success = await tm.cancel_task(task_id)
    if not success:
        raise HTTPException(
            status_code=409,
            detail=f"Task {task_id} is already finished (state={task.state.value})",
        )

    return {"task_id": task_id, "state": "cancelled"}


@router.get("/tasks")
async def list_tasks():
    """列出所有任务"""
    tm = get_task_manager()
    tasks = tm.list_tasks()
    return {
        "tasks": [t.to_dict() for t in tasks],
        "total": len(tasks),
    }


@router.get("/tasks/{task_id}/audit")
async def get_audit_log(task_id: str):
    """获取任务的审计日志"""
    from cua_server.services.task_policy import get_audit_logger
    audit = get_audit_logger()
    entries = audit.get_entries(task_id=task_id, limit=100)
    return {"task_id": task_id, "entries": entries, "total": len(entries)}
