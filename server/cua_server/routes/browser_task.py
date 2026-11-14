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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
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


# =============================================================
# 示教录制（Teach Recording）— 用户演示一遍，固化为新应用
# =============================================================

class StartRecordingRequest(BaseModel):
    name_hint: Optional[str] = ""


@router.post("/record/start")
async def start_recording(req: StartRecordingRequest):
    """开始示教录制：向当前页面注入 DOM 事件监听，开始捕获用户操作"""
    from cua_server.services.teach_recorder import start_recording
    result = start_recording(req.name_hint or "")
    if result.get("error"):
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@router.get("/record/peek")
async def peek_recording():
    """录制中实时查看已捕获的事件（前端轮询展示）"""
    from cua_server.services.teach_recorder import peek_recording
    result = peek_recording()
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/record/stop")
async def stop_recording():
    """停止录制，生成动作清单 + SOP 提示词"""
    from cua_server.services.teach_recorder import stop_recording
    result = stop_recording()
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/record/cancel")
async def cancel_recording():
    """取消录制，丢弃已捕获内容"""
    from cua_server.services.teach_recorder import cancel_recording
    return cancel_recording()


@router.post("/record/event")
async def record_event(request: Request):
    """接收页面 sendBeacon 上报的录制事件

    浏览器内页面通过 navigator.sendBeacon 即时上报每个用户操作。
    beacon 请求无 CORS 预检（规范豁免），即使跨 origin 也能送达。
    """
    from cua_server.services.teach_recorder import ingest_event
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(content={"ok": False}, status_code=200)
    if not isinstance(body, dict):
        return JSONResponse(content={"ok": False}, status_code=200)
    total = ingest_event(body)
    return JSONResponse(
        content={"ok": total >= 0, "total": total if total >= 0 else 0},
        status_code=200,
        headers={"Access-Control-Allow-Origin": "*"},
    )


# =============================================================
# 确定性回放（workflow-use 模式）：录制的命令序列确定性执行
# 单步失败 → 多策略自愈 → LLM 兜底；连续 3 步失败看门狗
# =============================================================

class ReplayRequest(BaseModel):
    commands: list  # 命令字符串列表，如 ["open https://...", "click #btn"]
    model: Optional[str] = None  # 可选：LLM 兜底用的模型


@router.post("/replay")
async def replay_commands(req: ReplayRequest):
    """确定性回放：逐条执行命令序列，返回 SSE 事件流

    事件类型：replay_start / replay_step(running|success|failed) /
    replay_watchdog / replay_abort / replay_done / error
    """
    if not req.commands:
        raise HTTPException(status_code=400, detail="commands 不能为空")

    from cua_server.services.replay_engine import run_replay

    model_config = {}
    if req.model:
        try:
            from cua_server.routes.hermes import _resolve_model_config, _write_hermes_config
            model_config = await _resolve_model_config(req.model) or {}
            if model_config.get("api_key"):
                _write_hermes_config(model_config)
            else:
                model_config = {}
        except Exception:
            model_config = {}

    async def event_stream():
        try:
            async for ev in run_replay(req.commands, req.model or "", model_config):
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "error": str(e)[:300]}, ensure_ascii=False) + "\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/view")
async def browser_view():
    """实时浏览器画面：CDP 全页截图 base64 PNG（前端每 ~800ms 轮询）。"""
    from cua_server.services.browser_driver import get_browser_driver
    driver = get_browser_driver()
    result = await driver.screenshot()
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "screenshot unavailable (check BROWSER_CDP_URL)")}
    return {"ok": True, "image": result["data"]}


@router.get("/heal-stats")
async def get_heal_statistics():
    """查询元素自愈统计（attempts/success/fail/by_strategy）"""
    from cua_server.services.replay_engine import get_heal_stats
    return get_heal_stats()


# =============================================================
# 录制模式交互透传：用户在画面面板上直接操作无头浏览器
# 点击/输入/滚轮 → agent-browser 坐标命令 → 页面真实响应
# =============================================================

class InteractRequest(BaseModel):
    action: str  # click | dblclick | type | key | wheel
    x: Optional[float] = None
    y: Optional[float] = None
    text: Optional[str] = None   # type 的文本
    key: Optional[str] = None    # key 的键名，如 Enter
    dy: Optional[float] = None   # wheel 滚动量


@router.post("/interact")
async def interact(req: InteractRequest):
    """录制模式下用户手动操作浏览器（画面坐标 / 键盘 / 滚轮透传）。

    每次操作前先用 elementFromPoint / activeElement 探测目标并**直写**录制事件，
    不依赖页面钩子：导航型点击会立刻销毁页面，钩子队列来不及回收。
    同时设置 __cuInteractUntil 抑制窗口，避免钩子上报同一动作造成重复步骤。
    """
    import asyncio
    import base64

    async def _cli(args: list, timeout: float = 15):
        proc = await asyncio.create_subprocess_exec(
            "agent-browser", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = (out or b"").decode("utf-8", "replace").strip()
        except asyncio.TimeoutError:
            return {"ok": False, "error": "timeout", "out": ""}
        return {"ok": proc.returncode == 0, "error": None if proc.returncode == 0 else out[:200], "out": out[:5000]}

    def _fix_mojibake(v):
        """agent-browser(Node) 把页面 UTF-8 文本按 latin-1 输出，中文会乱码。

        逐字段反转：同一结果里可能同时有正常中文（前端传入）和 mojibake（DOM 读取），
        整体 encode('latin-1') 会被正常中文打断。
        """
        if isinstance(v, str):
            try:
                return v.encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                return v
        if isinstance(v, list):
            return [_fix_mojibake(i) for i in v]
        if isinstance(v, dict):
            return {k: _fix_mojibake(x) for k, x in v.items()}
        return v

    async def _eval(js: str):
        """执行 JS 并两层解析返回值；dict/list 原样返回，标量转 str。"""
        b64 = base64.b64encode(js.encode("utf-8")).decode("ascii")
        r = await _cli(["eval", "-b", b64], timeout=12)
        raw = r.get("out") or ""
        if not r["ok"] or raw.startswith("error"):
            return ""
        try:
            data = json.loads(raw)
            if isinstance(data, str):
                data = json.loads(data)
            return _fix_mojibake(data)
        except Exception:
            return ""

    def _emit(ev: dict):
        try:
            _ingest_probe(ev)
        except Exception as e:
            logger.warning(f"[Interact] 直写录制事件失败: {str(e)[:120]}")

    # probe JS 一律保持扁平结构：嵌套 IIFE / try-catch 表达式经 base64 转传
    # 容易出现引号与括号转义问题，历史上曾导致静默失败。
    probe_js = (
        'JSON.stringify((function(){'
        ' window.__cuInteractUntil = Date.now() + 700;'
        f' var el = document.elementFromPoint({int(req.x or 0)}, {int(req.y or 0)});'
        ' if(!el) return null;'
        ' var t = el.closest("a,button,input,textarea,select,[role=button],[onclick]") || el;'
        ' var tag = t.tagName.toLowerCase();'
        ' var sib = t.parentElement ? t.parentElement.children : [];'
        ' var nth = 0, k = 0;'
        ' for(k=0;k<sib.length;k++){ if(sib[k].tagName===t.tagName) nth++; if(sib[k]===t) break; }'
        ' var sel = t.id ? ("#"+t.id) : (nth>1 ? tag+":nth-of-type("+nth+")" : tag);'
        ' var text = encodeURIComponent(((t.innerText||"")+""+(t.value||"")).trim().slice(0,40));'
        ' var ph = encodeURIComponent(t.placeholder || "");'
        ' var aria = encodeURIComponent(t.getAttribute("aria-label") || "");'
        ' var href = (tag==="a") ? (t.getAttribute("href")||"") : "";'
        ' var role = t.getAttribute("role") || "";'
        ' var onion = t.hasAttribute("onclick") ? 1 : 0;'
        ' var inter = (tag==="a"||tag==="button"||tag==="input"||tag==="textarea"'
        '  ||tag==="select"||role==="button"||onion===1) ? 1 : 0;'
        ' return {selector:sel, tag:tag, text:text, placeholder:ph, aria:aria,'
        '  href:href, interactive:inter};'
        '})())'
    )
    focus_js = (
        'JSON.stringify((function(){'
        ' window.__cuInteractUntil = Date.now() + 700;'
        ' var e = document.activeElement;'
        ' if(!e || e === document.body || !e.tagName) return null;'
        ' var tag = e.tagName.toLowerCase();'
        ' var sel = e.id ? ("#"+e.id) : tag;'
        ' return {selector:sel, tag:tag, placeholder: encodeURIComponent(e.placeholder || ""),'
        '  text: encodeURIComponent(((e.innerText||"")+""+(e.value||"")).trim().slice(0,40))};'
        '})())'
    )

    try:
        if req.action == "click":
            x, y = int(req.x or 0), int(req.y or 0)
            vp = await _eval('JSON.stringify({w:window.innerWidth,h:window.innerHeight})')
            if isinstance(vp, dict) and (x < 0 or y < 0 or x > vp.get("w", 0) or y > vp.get("h", 0)):
                return {"ok": False,
                        "error": f"坐标 ({x},{y}) 超出浏览器可视区域 "
                                 f"{vp.get('w')}x{vp.get('h')}，请先滚动页面"}
            probe = await _eval(probe_js)
            if not isinstance(probe, dict):
                logger.warning("[Interact] 元素探测失败（JS 执行异常），本次点击可能未被录制")
            for args in (["mouse", "move", str(x), str(y)],
                         ["mouse", "down", "left"], ["mouse", "up", "left"]):
                r = await _cli(args)
                if not r["ok"]:
                    return r
            captured = False
            if isinstance(probe, dict) and probe.get("interactive"):
                _emit({
                    "type": "click",
                    "selector": probe.get("selector", ""),
                    "describe": {"tag": probe.get("tag", ""),
                                 "text": probe.get("text", ""),
                                 "placeholder": probe.get("placeholder", ""),
                                 "aria": probe.get("aria", "")},
                    "href": probe.get("href") or None,
                })
                captured = True
            return {"ok": True, "captured": captured, "probe_ok": isinstance(probe, dict),
                    "out": r.get("out", "")}

        elif req.action == "type":
            focus = await _eval(focus_js)
            r = await _cli(["keyboard", "type", req.text or ""])
            if req.text and r["ok"]:
                sel = focus.get("selector", "") if isinstance(focus, dict) else ""
                _emit({
                    "type": "fill",
                    "selector": sel,
                    "describe": {"tag": (focus or {}).get("tag", "input") if isinstance(focus, dict) else "input",
                                 "placeholder": focus.get("placeholder", "") if isinstance(focus, dict) else "",
                                 "text": focus.get("text", "") if isinstance(focus, dict) else ""},
                    "value": (req.text or "")[:120],
                })
            return {"ok": r["ok"], "focus": bool(focus), "error": r.get("error"),
                    "hint": None if focus else "请先点击页面上的输入框再输入文字"}

        elif req.action == "key":
            focus = await _eval(focus_js)
            key = req.key or "Enter"
            r = await _cli(["press", key])
            if r["ok"]:
                sel = focus.get("selector", "") if isinstance(focus, dict) else ""
                if key == "Enter":
                    _emit({"type": "press_enter", "selector": sel,
                           "describe": {"tag": "input", "text": ""}})
                else:
                    _emit({"type": "key", "key": key, "selector": sel,
                           "describe": {"tag": "input", "text": ""}})
            return r

        elif req.action == "wheel":
            await _eval("window.__cuInteractUntil = Date.now() + 700; 'ok'")
            dy = int(req.dy or 0)
            r = await _cli(["mouse", "wheel", str(dy)])
            if r["ok"]:
                _emit({"type": "scroll", "dy": dy, "selector": "", "describe": {"tag": "body"}})
            return r

        else:
            raise HTTPException(status_code=400, detail=f"未知操作: {req.action}")
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _ingest_probe(ev: dict):
    """交互透传产生的录制事件直写会话（描述结构与页面钩子一致）。"""
    from cua_server.services.teach_recorder import ingest_event
    ingest_event(ev)
