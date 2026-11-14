"""
Hermes Agent Pool — 持久化 AIAgent 进程池

通过持久化子进程（运行在 Hermes venv 中）复用 AIAgent 实例，
避免每次请求都重新初始化 Hermes CLI，将初始化开销从 ~5.9s 降至 ~2.8s（首次），
后续请求 0ms 初始化开销。

架构:
  ontomind-agent (我们的 Python venv)
    └── HermesAgentPool (本模块)
         └── asyncio.subprocess → hermes_bridge.py (Hermes venv)
              └── AIAgent (持久化，可复用）

v2: 使用 asyncio.create_subprocess_exec 替代 subprocess.Popen，
    避免 run_in_executor 与 FastAPI StreamingResponse 的兼容性问题。

v3 (P1): 单读者分发器 — 一个 asyncio Task 统一读 stdout，
         按 data["id"] 路由到各请求的 asyncio.Queue。
         每模型并发限制 Semaphore(1)。
         三级超时：任务 600s / 模型调用 120s / 工具执行 60s。
         超时自动 terminate + 重建 bridge。
"""

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, Dict, AsyncGenerator

from src.ontomind_agent.utils.logger import logger


BRIDGE_SCRIPT = os.path.join(
    os.path.dirname(__file__), "hermes_bridge.py"
)

# ── 三级超时配置 (CU-103) ──────────────────────────────────────────
TIMEOUT_TASK = 600      # 整个任务最长等待
TIMEOUT_MODEL = 120     # 单次模型调用
TIMEOUT_TOOL = 60       # 单次工具执行
TIMEOUT_INIT = 60       # bridge 初始化


def _find_hermes_python() -> Optional[str]:
    """查找 Hermes venv 中的 Python 解释器路径"""
    candidates = [
        os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python"),
        os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python3"),
        "/home/ontomind/.hermes/hermes-agent/venv/bin/python",
        "/home/ontomind/.hermes/hermes-agent/venv/bin/python3",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


@dataclass
class BridgeProcess:
    """管理一个 Hermes Bridge 子进程

    v3: 内置单读者分发器（CU-101），所有 stdout 读取由 _dispatch_task 统一完成，
        按 data["id"] 路由到 _pending_requests[req_id] 的 asyncio.Queue。
    """
    process: asyncio.subprocess.Process
    model_key: str  # "model@base_url"
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)
    chat_count: int = 0
    init_ms: int = 0
    tool_count: int = 0
    _stderr_lines: list = field(default_factory=list)
    _stderr_task: Optional[asyncio.Task] = None
    # CU-101: 单读者分发器
    _dispatch_task: Optional[asyncio.Task] = None
    _pending_requests: Dict[str, asyncio.Queue] = field(default_factory=dict)

    def is_alive(self) -> bool:
        return self.process.returncode is None

    def start_dispatcher(self):
        """启动 stdout 单读者分发任务（CU-101）

        唯一读取 process.stdout.readline() 的 asyncio Task，
        按 data["id"] 路由到对应请求的 asyncio.Queue。
        """
        if self._dispatch_task and not self._dispatch_task.done():
            return  # 已启动
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())

    async def _dispatch_loop(self):
        """单读者循环：唯一消费 stdout 的任务

        读一行 → JSON 解析 → 按 data["id"] 路由到 _pending_requests[req_id] 队列。
        无匹配 req_id 的行（如全局日志）跳过。
        """
        try:
            while True:
                if self.process.returncode is not None:
                    # 进程已退出，通知所有等待中的请求
                    for req_id, q in self._pending_requests.items():
                        await q.put({"type": "error",
                                     "error": "Bridge process died (exit={})".format(
                                         self.process.returncode)})
                    self._pending_requests.clear()
                    return

                try:
                    line_bytes = await asyncio.wait_for(
                        self.process.stdout.readline(),
                        timeout=5,
                    )
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error("[Pool] dispatch readline error: {}".format(e))
                    continue

                if not line_bytes:
                    if self.process.returncode is not None:
                        for req_id, q in self._pending_requests.items():
                            await q.put({"type": "error",
                                         "error": "Bridge process exited"})
                        self._pending_requests.clear()
                        return
                    await asyncio.sleep(0.05)
                    continue

                line = line_bytes.decode("utf-8", errors="replace")
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    logger.warning("[Pool] Invalid JSON from bridge: {}".format(line[:200]))
                    continue

                req_id = data.get("id", "")
                if req_id and req_id in self._pending_requests:
                    await self._pending_requests[req_id].put(data)
                    # type=result 表示该请求完成，清理队列引用
                    if data.get("type") == "result":
                        del self._pending_requests[req_id]
                else:
                    # 无匹配请求的行（可能是全局日志或已超时清理的请求）
                    logger.debug("[Pool] Unmatched stdout line (id={}): {}".format(
                        req_id or "(none)", line[:150]))

        except asyncio.CancelledError:
            # 被 terminate() 取消，正常退出
            pass
        except Exception as e:
            logger.error("[Pool] dispatch loop crashed: {}".format(e))

    def register_request(self, req_id: str) -> asyncio.Queue:
        """注册一个新请求，返回专属事件队列（CU-101）"""
        q: asyncio.Queue = asyncio.Queue()
        self._pending_requests[req_id] = q
        return q

    async def terminate(self):
        """Gracefully terminate the bridge process and all background tasks."""
        # Cancel dispatch task first (CU-101)
        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except (asyncio.CancelledError, Exception):
                pass
            self._dispatch_task = None

        # Cancel stderr reader task
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stderr_task = None

        # Send shutdown command
        try:
            if self.process.returncode is None and self.process.stdin:
                cmd = json.dumps({"id": "shutdown", "cmd": "shutdown"}, ensure_ascii=False) + "\n"
                self.process.stdin.write(cmd.encode())
                await self.process.stdin.drain()
        except Exception:
            pass

        # Wait for graceful exit
        try:
            await asyncio.wait_for(self.process.wait(), timeout=3)
        except (asyncio.TimeoutError, Exception):
            pass

        # Force kill if still alive
        try:
            if self.process.returncode is None:
                self.process.kill()
                await self.process.wait()
        except Exception:
            pass


class HermesAgentPool:
    """持久化 AIAgent 进程池

    使用方式:
        pool = HermesAgentPool()

        # 初始化 agent（首次或模型切换时）
        await pool.init_agent(model_key, model_config)

        # 对话
        async for event in pool.chat(model_key, message, session_id):
            yield event

        # 清理
        await pool.cleanup()
    """

    def __init__(self, max_idle_seconds: int = 600, max_bridges: int = 3):
        self._bridges: Dict[str, BridgeProcess] = {}
        self._max_idle_seconds = max_idle_seconds
        self._max_bridges = max_bridges
        self._lock = asyncio.Lock()
        # CU-102: 每模型并发限制
        self._semaphores: Dict[str, asyncio.Semaphore] = {}

    def _get_semaphore(self, model_key: str) -> asyncio.Semaphore:
        """获取指定模型的并发信号量（CU-102），每模型同时只允许 1 个请求"""
        if model_key not in self._semaphores:
            self._semaphores[model_key] = asyncio.Semaphore(1)
        return self._semaphores[model_key]

    async def _get_or_start_bridge(self, model_key: str, model_config: dict) -> tuple:
        """获取或创建指定 model_key 对应的 bridge 进程

        Returns:
            (BridgeProcess, is_cached: bool)
        """
        # Fast path: check cache without creating
        async with self._lock:
            if model_key in self._bridges:
                bridge = self._bridges[model_key]
                if bridge.is_alive():
                    bridge.last_used_at = time.monotonic()
                    return bridge, True
                else:
                    logger.warning("[Pool] Bridge for {} is dead, restarting".format(model_key))
                    del self._bridges[model_key]

            # Evict if needed (inside lock)
            if len(self._bridges) >= self._max_bridges:
                await self._evict_one()

        # Create bridge outside lock to avoid blocking other callers
        bridge = await self._create_bridge(model_key, model_config)

        # Insert into cache (back under lock)
        async with self._lock:
            # Check if another caller already created it while we were building
            if model_key in self._bridges and self._bridges[model_key].is_alive():
                logger.info("[Pool] Another caller already created bridge for {}".format(model_key))
                await bridge.terminate()  # Discard our duplicate
                return self._bridges[model_key], True

            self._bridges[model_key] = bridge
            return bridge, False

    async def _create_bridge(self, model_key: str, model_config: dict) -> BridgeProcess:
        """创建新的 bridge 子进程并初始化 AIAgent"""
        python_bin = _find_hermes_python()
        if not python_bin:
            raise RuntimeError("Hermes venv Python not found")

        if not os.path.isfile(BRIDGE_SCRIPT):
            raise RuntimeError("Bridge script not found: {}".format(BRIDGE_SCRIPT))

        t0 = time.monotonic()

        # 使用 asyncio 原生子进程
        process = await asyncio.create_subprocess_exec(
            python_bin, "-u", BRIDGE_SCRIPT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        bridge = BridgeProcess(process=process, model_key=model_key)

        # 启动后台 stderr 消费任务
        async def _read_stderr():
            stderr_lines = bridge._stderr_lines
            try:
                while True:
                    line = await process.stderr.readline()
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="replace")
                    stderr_lines.append(decoded)
                    if len(stderr_lines) > 200:
                        stderr_lines.pop(0)
            except Exception:
                pass

        bridge._stderr_task = asyncio.create_task(_read_stderr())

        # CU-101: 启动单读者分发器
        bridge.start_dispatcher()

        logger.info("[Pool] Bridge process started, pid={}".format(process.pid))

        # 发送 init 命令
        init_req_id = "init_{}".format(uuid.uuid4().hex[:8])
        # P4: 使用 Computer Use 的 disabled_toolsets 配置
        from src.ontomind_agent.services.computer_use_system import DISABLED_TOOLSETS
        init_params = {
            "id": init_req_id,
            "cmd": "init",
            "model": model_config.get("model", ""),
            "base_url": model_config.get("api_base", ""),
            "api_key": model_config.get("api_key", ""),
            "disabled_toolsets": DISABLED_TOOLSETS,
            "max_iterations": 90,
        }

        await self._send_cmd(bridge.process, init_params)

        # 等待 init 响应（通过分发器队列）
        resp = await self._read_response_via_queue(bridge, init_req_id, timeout=TIMEOUT_INIT)
        if resp.get("status") != "ok":
            raise RuntimeError("Bridge init failed: {}".format(resp.get("error", "unknown")))

        bridge.init_ms = resp.get("init_ms", 0)
        bridge.tool_count = resp.get("tool_count", 0)
        bridge.last_used_at = time.monotonic()

        t1 = time.monotonic()
        logger.info(
            "[Pool] Bridge ready: model={}, init={}ms, tools={}, total={:.0f}ms".format(
                model_config.get("model"), bridge.init_ms, bridge.tool_count, (t1 - t0) * 1000
            )
        )

        return bridge

    async def _evict_one(self):
        """淘汰最久未使用的 bridge"""
        if not self._bridges:
            return
        oldest_key = min(self._bridges.keys(),
                         key=lambda k: self._bridges[k].last_used_at)
        logger.info("[Pool] Evicting idle bridge: {}".format(oldest_key))
        bridge = self._bridges.pop(oldest_key)
        await bridge.terminate()

    async def cleanup(self):
        """清理闲置过久的 bridge"""
        async with self._lock:
            now = time.monotonic()
            to_remove = []
            for key, bridge in self._bridges.items():
                if now - bridge.last_used_at > self._max_idle_seconds:
                    to_remove.append(key)
            for key in to_remove:
                logger.info("[Pool] Cleaning up idle bridge: {}".format(key))
                bridge = self._bridges.pop(key)
                await bridge.terminate()

    async def close_all(self):
        """关闭所有 bridge 进程"""
        async with self._lock:
            for key, bridge in list(self._bridges.items()):
                await bridge.terminate()
            self._bridges.clear()
            logger.info("[Pool] All bridges closed")

    # ── Public API ──────────────────────────────────────────────────

    async def init_agent(self, model_key: str, model_config: dict) -> dict:
        """预初始化指定模型的 agent（可选，不调用也会在首次 chat 时自动初始化）

        Returns:
            dict: {"init_ms": 1234, "tool_count": 28, "cached": False}
        """
        bridge, is_cached = await self._get_or_start_bridge(model_key, model_config)
        return {"init_ms": bridge.init_ms, "tool_count": bridge.tool_count, "cached": is_cached}

    async def chat(
        self,
        model_key: str,
        model_config: dict,
        message: str,
        session_id: str = "",
        ephemeral_system_prompt: str = "",
    ) -> AsyncGenerator[dict, None]:
        """执行对话，返回事件流

        v3 (P1): 通过单读者分发器队列读取事件，不再直接读 stdout。
        每模型并发限制 Semaphore(1)。
        三级超时：任务 600s / 模型 120s / 工具 60s。

        Args:
            model_key: 模型标识
            model_config: 模型配置
            message: 用户消息
            session_id: 会话 ID
            ephemeral_system_prompt: 可选的临时系统提示词

        Yields:
            dict: 事件对象
        """
        # CU-102: 每模型并发限制
        sem = self._get_semaphore(model_key)
        async with sem:
            bridge, _ = await self._get_or_start_bridge(model_key, model_config)
            bridge.last_used_at = time.monotonic()
            bridge.chat_count += 1

            req_id = "chat_{}".format(uuid.uuid4().hex[:8])

            chat_params = {
                "id": req_id,
                "cmd": "chat",
                "message": message,
                "session_id": session_id,
            }
            if ephemeral_system_prompt:
                chat_params["ephemeral_system_prompt"] = ephemeral_system_prompt

            await self._send_cmd(bridge.process, chat_params)

            # CU-101: 通过分发器队列读取事件
            queue = bridge.register_request(req_id)

            # CU-103: 三级超时 — 整体任务超时
            deadline = time.monotonic() + TIMEOUT_TASK
            last_event_time = time.monotonic()

            try:
                while time.monotonic() < deadline:
                    remaining = min(TIMEOUT_MODEL, deadline - time.monotonic())
                    if remaining <= 0:
                        yield {"type": "error",
                               "error": "Task timeout after {}s".format(TIMEOUT_TASK)}
                        return

                    try:
                        data = await asyncio.wait_for(queue.get(), timeout=remaining)
                    except asyncio.TimeoutError:
                        # CU-104: 超时清理 + bridge 重建
                        idle = time.monotonic() - last_event_time
                        if idle >= TIMEOUT_TASK:
                            logger.error(
                                "[Pool] Task timeout req_id={} idle={:.0f}s, rebuilding bridge".format(
                                    req_id, idle))
                            await self._rebuild_bridge(model_key, model_config, bridge)
                            yield {"type": "error",
                                   "error": "Task timeout after {}s".format(TIMEOUT_TASK)}
                            return
                        continue

                    last_event_time = time.monotonic()

                    # 检查进程是否意外终止
                    if data.get("type") == "error" and "died" in data.get("error", ""):
                        logger.error("[Pool] Bridge died for req_id={}: {}".format(
                            req_id, data.get("error")))
                        yield data
                        return

                    yield data

                    if data.get("type") == "result":
                        return

                yield {"type": "error", "error": "Chat timeout after {}s".format(TIMEOUT_TASK)}

            except asyncio.CancelledError:
                # 请求被取消（如前端 stop），清理队列
                bridge._pending_requests.pop(req_id, None)
                raise

    async def ping(self, model_key: str) -> bool:
        """检查指定模型的 bridge 是否存活"""
        if model_key not in self._bridges:
            return False
        bridge = self._bridges[model_key]
        if not bridge.is_alive():
            return False

        req_id = "ping_{}".format(uuid.uuid4().hex[:8])
        await self._send_cmd(bridge.process, {"id": req_id, "cmd": "ping"})
        try:
            resp = await self._read_response_via_queue(bridge, req_id, timeout=5)
            return resp.get("status") == "ok"
        except Exception:
            return False

    # ── Internal Helpers ────────────────────────────────────────────

    async def _send_cmd(self, process: asyncio.subprocess.Process, cmd: dict):
        """发送 JSON 命令到 bridge 进程"""
        line = json.dumps(cmd, ensure_ascii=False) + "\n"
        try:
            process.stdin.write(line.encode())
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise RuntimeError("Bridge process pipe broken")

    async def _read_response_via_queue(self, bridge: BridgeProcess, req_id: str,
                                       timeout: int = TIMEOUT_MODEL) -> dict:
        """通过分发器队列读取指定 req_id 的 result 响应（CU-101）

        替代旧的 _read_response（直接读 stdout）。
        """
        queue = bridge.register_request(req_id)
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if bridge.process.returncode is not None:
                stderr_tail = ""
                if bridge._stderr_lines:
                    stderr_tail = "".join(bridge._stderr_lines[-20:])[-500:]
                raise RuntimeError(
                    "Bridge process died (exit={}). stderr: {}".format(
                        bridge.process.returncode, stderr_tail))

            try:
                data = await asyncio.wait_for(
                    queue.get(),
                    timeout=min(5, deadline - time.monotonic())
                )
            except asyncio.TimeoutError:
                continue

            if data.get("id") == req_id and data.get("type") == "result":
                return data

        stderr_tail = ""
        if bridge._stderr_lines:
            stderr_tail = "".join(bridge._stderr_lines[-30:])[-1000:]
        raise RuntimeError(
            "Bridge response timeout after {}s for req_id={}. stderr tail: {}".format(
                timeout, req_id, stderr_tail))

    async def _rebuild_bridge(self, model_key: str, model_config: dict,
                              old_bridge: BridgeProcess):
        """CU-104: 超时/死亡时 terminate 旧 bridge 并从缓存移除，下次请求自动重建"""
        logger.warning("[Pool] Rebuilding bridge for model_key={}".format(model_key))
        try:
            await old_bridge.terminate()
        except Exception as e:
            logger.error("[Pool] Error terminating old bridge: {}".format(e))

        async with self._lock:
            if model_key in self._bridges and self._bridges[model_key] is old_bridge:
                del self._bridges[model_key]
            # 清理旧信号量，下次请求会重建
            self._semaphores.pop(model_key, None)


# 全局单例
_pool: Optional[HermesAgentPool] = None


def get_hermes_pool() -> HermesAgentPool:
    """获取全局 HermesAgentPool 单例"""
    global _pool
    if _pool is None:
        _pool = HermesAgentPool()
    return _pool


async def shutdown_hermes_pool():
    """应用关闭时清理所有 bridge 进程"""
    global _pool
    if _pool is not None:
        await _pool.close_all()
        _pool = None
        logger.info("[Pool] Hermes agent pool shut down")
