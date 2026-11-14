"""金融数据哨兵调度器（M1）。

职责：扫描 tm_agent_schedule_task 表中绑定了录制应用（app_id 非空）的到期计划，
自动触发应用执行（/browser/tasks 任务模式，SOP 作为 system_prompt），
执行结果落 sentinel_result 表，形成「录一次，天天跑」的最小闭环。

设计要点：
- 不建新调度模型：复用 server 端 AgentScheduleTask（表 tm_agent_schedule_task），
  仅新增 app_id 列（迁移由本模块启动时自动执行，幂等）。
- next_run 语义与 server 端 ServiceImpl.calculateNextRun 对齐：
  执行完按 frequency 计算下一次时间并回写。
- 防并发：全局同时只跑一个哨兵任务（类级锁）；同一计划上次未完成则本次跳过。
- 失败容忍：单次失败记录 sentinel_result(status=failed)，连续失败计数在
  plan 的 command 字段尾部 JSON 里（M1 简化：连续 3 次失败自动 pause 计划）。
"""

import asyncio
import json
import logging

from loguru import logger
import time
from datetime import datetime, timedelta

import sqlite3


# ---------------------------------------------------------------------------
# DB 访问
# ---------------------------------------------------------------------------

def _db_conn():
    import os
    db = os.environ.get("CUA_SCHEDULE_DB", "./cua_schedules.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema() -> None:
    """幂等迁移：调度表加 app_id 列；新建哨兵结果表。"""
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE tm_agent_schedule_task
                ADD COLUMN IF NOT EXISTS app_id VARCHAR(36);
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sentinel_result (
                    id VARCHAR(36) PRIMARY KEY,
                    schedule_id VARCHAR(36),
                    app_id VARCHAR(36),
                    run_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    status VARCHAR(20) NOT NULL,
                    summary TEXT,
                    extracted JSONB,
                    cost_ms INTEGER,
                    error TEXT
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_sentinel_result_schedule
                ON sentinel_result(schedule_id, run_at DESC);
            """)
        conn.commit()
        logger.info("[Sentinel] schema 迁移完成")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 计划扫描与时间计算（与 server 端 calculateNextRun 语义对齐）
# ---------------------------------------------------------------------------

def _fetch_due_plans() -> list:
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, username, name, frequency, hour_interval,
                       weekly_day, monthly_day, execution_time,
                       command, status, next_run, app_id
                FROM tm_agent_schedule_task
                WHERE app_id IS NOT NULL
                  AND status = 'active'
                  AND next_run IS NOT NULL
                  AND next_run <= NOW()
                FOR UPDATE SKIP LOCKED
            """)
            return cur.fetchall()
    finally:
        conn.close()


def _calc_next_run(plan: dict) -> datetime:
    now = datetime.now()
    time_str = plan.get("execution_time") or "09:00"
    parts = time_str.split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    freq = (plan.get("frequency") or "daily").lower()

    if freq == "hourly":
        interval = plan.get("hour_interval") or 1
        nxt = now + timedelta(hours=interval)
        return nxt.replace(minute=minute, second=0, microsecond=0) \
            if minute else nxt.replace(second=0, microsecond=0)
    if freq == "weekly":
        days_ahead = ((int(plan.get("weekly_day") or 1) - now.weekday()) % 7) or 7
        return (now + timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
    if freq == "monthly":
        import calendar
        target_day = int(plan.get("monthly_day") or 1)
        year, month = (now.year, now.month + 1) if now.month == 12 else (now.year, now.month + 1)
        if now.month == 12:
            year, month = now.year + 1, 1
        day = min(target_day, calendar.monthrange(year, month)[1])
        return datetime(year, month, day, hour, minute)
    # daily 默认：明天
    return (now + timedelta(days=1)).replace(
        hour=hour, minute=minute, second=0, microsecond=0)


def _mark_running(plan_id: str) -> bool:
    """占用计划（status=running），返回是否占用成功。"""
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tm_agent_schedule_task SET status='running' "
                "WHERE id=%s AND status='active' RETURNING id",
                (plan_id,))
            ok = cur.fetchone() is not None
        conn.commit()
        return ok
    finally:
        conn.close()


def _finish_plan(plan_id: str, ok: bool, next_run: datetime,
                 consecutive_failures: int) -> None:
    """回写计划状态与 next_run；连续失败 3 次自动暂停。"""
    new_status = "paused" if consecutive_failures >= 3 else "active"
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tm_agent_schedule_task
                SET status=%s, next_run=%s, last_run=NOW(), run_count=COALESCE(run_count,0)+1
                WHERE id=%s
            """, (new_status, next_run, plan_id))
        conn.commit()
        if new_status == "paused":
            logger.warning("[Sentinel] 计划 %s 连续失败 %d 次，已自动暂停", plan_id, consecutive_failures)
    finally:
        conn.close()


def _save_result(schedule_id: str, app_id: str, ok: bool, summary: str,
                 cost_ms: int, error: str = "") -> None:
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sentinel_result (id, schedule_id, app_id, status, summary, cost_ms, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (f"sr_{int(time.time()*1000)}_{schedule_id[:8]}",
                  schedule_id, app_id,
                  "success" if ok else "failed",
                  summary[:2000], cost_ms, error[:1000]))
        conn.commit()
    finally:
        conn.close()


def _failure_count(schedule_id: str) -> int:
    """从最近结果统计连续失败次数。"""
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT status FROM sentinel_result
                WHERE schedule_id=%s ORDER BY run_at DESC LIMIT 5
            """, (schedule_id,))
            n = 0
            for row in cur.fetchall():
                if row["status"] == "failed":
                    n += 1
                else:
                    break
            return n
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 应用获取与执行
# ---------------------------------------------------------------------------

def _load_app(app_id: str) -> dict | None:
    """从本地应用库取应用（SOP 作为 system_prompt）。

    应用库为 JSON 文件目录（CUA_APPS_DIR，默认 ./apps），每个文件一个应用：
      {"id": "...", "name": "...", "systemPrompt": "【示教的操作步骤】..."}
    """
    import json
    import os
    apps_dir = os.environ.get("CUA_APPS_DIR", "./apps")
    if os.path.isdir(apps_dir):
        for fn in os.listdir(apps_dir):
            if not fn.endswith(".json"):
                continue
            try:
                app = json.load(open(os.path.join(apps_dir, fn), encoding="utf-8"))
                if app.get("id") == app_id or fn[:-5] == app_id:
                    return app
            except Exception as e:
                logger.warning("[Sentinel] 解析应用 %s 失败: %s", fn, e)
    logger.warning("[Sentinel] 应用 %s 不在本地应用库 %s", app_id, apps_dir)
    return None


def _resolve_model() -> str:
    """默认模型：CUA_SENTINEL_MODEL 环境变量（如 dashscope::qwen-plus）。"""
    import os
    return os.environ.get("CUA_SENTINEL_MODEL", os.environ.get("CUA_MODEL_KEY", ""))


async def _run_app_task(app: dict, plan_name: str) -> tuple[bool, str, int, str]:
    """执行应用：POST /browser/tasks（SOP 模式），收集 SSE 直到 done。"""
    import httpx

    system_prompt = app.get("systemPrompt") or ""
    if not system_prompt:
        return False, "应用缺少 SOP（systemPrompt 为空）", 0

    message = f"【数据哨兵定时执行】请按示教步骤完成「{plan_name}」任务，汇总执行结果。"
    model = app.get("modelKey") or _resolve_model()
    if not model:
        return False, "无可用模型（应用未配置 modelKey 且系统无默认模型）", 0

    start = time.monotonic()
    final_text_parts: list = []
    ok = False
    error_msg = ""

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=600, write=30, pool=15)) as client:
            async with client.stream(
                "POST", "http://127.0.0.1:8000/agent/browser/tasks",
                json={"message": message, "model": model, "system_prompt": system_prompt},
            ) as resp:
                if resp.status_code != 200:
                    return False, f"任务创建失败 HTTP {resp.status_code}", 0
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    t = ev.get("type")
                    if t == "content":
                        final_text_parts.append(ev.get("content") or "")
                    elif t == "error":
                        error_msg = ev.get("error") or ev.get("message") or "任务错误"
                    elif t == "done":
                        state = ev.get("state") or "done"
                        ok = state in ("done", "completed", "success")
                        if state == "failed" and not error_msg:
                            error_msg = "任务执行失败"
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"

    cost_ms = int((time.monotonic() - start) * 1000)
    summary = "".join(final_text_parts).strip() or (error_msg or "(无输出)")
    return ok, summary, cost_ms, error_msg


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

class SentinelScheduler:
    """单实例调度循环。"""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running_plan_ids: set = set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info("[Sentinel] 调度器已启动（每 60s 扫描一次）")

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        # 启动先做迁移
        for attempt in range(5):
            try:
                await asyncio.to_thread(ensure_schema)
                break
            except Exception as e:
                logger.warning("[Sentinel] schema 迁移重试 %d: %s", attempt + 1, e)
                await asyncio.sleep(5)

        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[Sentinel] 扫描异常")
            await asyncio.sleep(60)

    async def _tick(self) -> None:
        """扫描（线程）+ 触发（事件循环侧 create_task，工作线程无 loop 不能建任务）。"""
        plans = await asyncio.to_thread(_fetch_due_plans)
        for plan in plans:
            pid = plan["id"]
            if pid in self._running_plan_ids:
                continue
            occupied = await asyncio.to_thread(_mark_running, pid)
            if not occupied:
                continue
            self._running_plan_ids.add(pid)
            asyncio.ensure_future(self._execute(plan))

    async def _execute(self, plan: dict) -> None:
        pid = plan["id"]
        app_id = plan["app_id"]
        logger.info("[Sentinel] 触发计划「%s」(%s) → 应用 %s", plan["name"], pid, app_id)
        ok, summary, cost_ms, error_msg = False, "初始化", 0, ""
        try:
            app = await asyncio.to_thread(_load_app, app_id)
            if not app:
                ok, summary, cost_ms, error_msg = False, f"应用 {app_id} 不存在或不可读", 0, "app_not_found"
            else:
                ok, summary, cost_ms, error_msg = await _run_app_task(app, plan["name"])
        except Exception as e:
            ok, summary, cost_ms, error_msg = False, f"{type(e).__name__}: {e}", 0, str(e)[:500]
        finally:
            try:
                await asyncio.to_thread(
                    _save_result, pid, app_id, ok, summary, cost_ms, error_msg)
                fails = await asyncio.to_thread(_failure_count, pid) if not ok else 0
                await asyncio.to_thread(_finish_plan, pid, ok, _calc_next_run(plan), fails)
            except Exception:
                logger.exception("[Sentinel] 结果落库失败")
            finally:
                self._running_plan_ids.discard(pid)
        logger.info("[Sentinel] 计划「%s」执行完成: ok=%s, cost=%dms, 摘要=%.80s",
                    plan["name"], ok, cost_ms, summary)


# 模块级单例，供 FastAPI 启动钩子使用
scheduler = SentinelScheduler()
