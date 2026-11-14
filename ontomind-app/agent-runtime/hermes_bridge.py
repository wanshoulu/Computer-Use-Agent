#!/usr/bin/env python3
"""
Hermes Agent Bridge — 持久化 AIAgent 进程

运行在 Hermes venv 中，通过 stdin/stdout JSON 协议与 ontomind-agent 服务通信。
每行一个 JSON 对象，请求和事件均以换行符分隔。

协议:
  → {"id": "req_1", "cmd": "init", "model": "...", "base_url": "...", "api_key": "..."}
  ← {"id": "req_1", "type": "result", "status": "ok", "init_ms": 1234}

  → {"id": "req_2", "cmd": "chat", "message": "hi", "session_id": "..."}
  ← {"id": "req_2", "type": "tool_start", "name": "search", "message": "..."}
  ← {"id": "req_2", "type": "tool_end", "name": "search", "duration_ms": 1200}
  ← {"id": "req_2", "type": "thinking", "content": "..."}
  ← {"id": "req_2", "type": "result", "status": "ok", "response": "full text"}

  → {"id": "req_3", "cmd": "ping"}
  ← {"id": "req_3", "type": "result", "status": "ok"}

  → {"id": "req_4", "cmd": "shutdown"}
  ← {"id": "req_4", "type": "result", "status": "ok"}
"""

import json
import os
import sys
import time
import traceback
import threading
from datetime import datetime

# ── Hermes imports (may fail if hermes-agent is not installed) ──────────
try:
    from cli import AIAgent  # noqa: E402
    _HERMES_AVAILABLE = True
except ImportError as e:
    AIAgent = None
    _HERMES_AVAILABLE = False
    _HERMES_IMPORT_ERROR = str(e)

try:
    from tools.mcp_tool import discover_mcp_tools  # noqa: E402
    _MCP_AVAILABLE = True
except ImportError as e:
    discover_mcp_tools = None
    _MCP_AVAILABLE = False
    _MCP_IMPORT_ERROR = str(e)

# Ensure Hermes home is set (don't override if already configured)
os.environ.setdefault("HERMES_HOME", os.path.expanduser("~/.hermes"))

# Redirect stdout to stderr for all logging from Hermes modules
# We reserve stdout exclusively for the JSON protocol
_real_stdout = sys.stdout
sys.stdout = sys.stderr


def _json_response(obj):
    """Write a JSON object to the real stdout, followed by newline, then flush."""
    line = json.dumps(obj, ensure_ascii=False, default=str)
    _real_stdout.write(line + "\n")
    _real_stdout.flush()


class HermesBridge:
    """Manages a single AIAgent instance and routes chat requests."""

    def __init__(self):
        self.agent = None
        self.current_model_key = None
        self._mcp_discovered = False
        self._chat_lock = threading.Lock()

    def _ensure_mcp(self):
        """Ensure MCP tools are discovered (one-time operation)."""
        if self._mcp_discovered:
            return
        if not _MCP_AVAILABLE:
            print(f"[Bridge] MCP SDK not available: {_MCP_IMPORT_ERROR}", file=sys.stderr)
            self._mcp_discovered = True  # Don't retry
            return
        try:
            t0 = time.monotonic()
            mcp_tools = discover_mcp_tools()
            t1 = time.monotonic()
            print(f"[Bridge] MCP tools discovered: {len(mcp_tools)} tools in {(t1-t0)*1000:.0f}ms",
                  file=sys.stderr)
            self._mcp_discovered = True
        except Exception as e:
            print(f"[Bridge] MCP discovery failed: {e}", file=sys.stderr)
            self._mcp_discovered = True  # Don't retry on failure

    def handle_init(self, req_id: str, params: dict):
        """Initialize or re-initialize the AIAgent with given model config."""
        if not _HERMES_AVAILABLE:
            _json_response({"id": req_id, "type": "result", "status": "error",
                           "error": f"Hermes CLI not installed: {_HERMES_IMPORT_ERROR}"})
            return

        model = params.get("model", "")
        base_url = params.get("base_url", "")
        api_key = params.get("api_key", "")
        disabled_toolsets = params.get("disabled_toolsets", ["tts"])
        max_iterations = params.get("max_iterations", 90)

        model_key = f"{model}@{base_url}"

        # If same model already initialized, skip
        if self.agent is not None and self.current_model_key == model_key:
            _json_response({"id": req_id, "type": "result", "status": "ok",
                           "init_ms": 0, "cached": True})
            return

        t0 = time.monotonic()

        # Ensure MCP is discovered before creating agent
        self._ensure_mcp()

        try:
            self.agent = AIAgent(
                model=model,
                base_url=base_url,
                api_key=api_key,
                disabled_toolsets=disabled_toolsets,
                max_iterations=max_iterations,
                quiet_mode=True,
                # Tool callbacks for trace events
                tool_start_callback=self._on_tool_start,
                tool_complete_callback=self._on_tool_complete,
                thinking_callback=self._on_thinking,
            )
            self.current_model_key = model_key
            self._current_req_id = req_id  # for callbacks to reference

            t1 = time.monotonic()
            init_ms = int((t1 - t0) * 1000)
            tool_count = len(self.agent.tools) if hasattr(self.agent, "tools") else 0
            print(f"[Bridge] AIAgent init: {init_ms}ms, {tool_count} tools, model={model}",
                  file=sys.stderr)

            _json_response({"id": req_id, "type": "result", "status": "ok",
                           "init_ms": init_ms, "tool_count": tool_count})
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            _json_response({"id": req_id, "type": "result", "status": "error",
                           "error": str(e)})

    def handle_chat(self, req_id: str, params: dict):
        """Execute a chat request on the current agent (thread-safe).

        Supports optional ``ephemeral_system_prompt`` parameter — injected into
        the agent's system prompt for this request only (not persisted). Used to
        pass skill instructions so the agent treats them as mandatory directives
        rather than soft suggestions in the user message.

        CU-105: 结构化日志 — 记录 task_id/duration_ms/tools_called，不记录密钥。
        """
        if self.agent is None:
            _json_response({"id": req_id, "type": "result", "status": "error",
                           "error": "Agent not initialized. Send 'init' first."})
            return

        message = params.get("message", "")
        session_id = params.get("session_id", "")
        ephemeral_system_prompt = params.get("ephemeral_system_prompt", "")

        if not message.strip():
            _json_response({"id": req_id, "type": "result", "status": "error",
                           "error": "Empty message"})
            return

        # Use lock to prevent concurrent chat calls on the same agent instance
        acquired = self._chat_lock.acquire(timeout=300)
        if not acquired:
            _json_response({"id": req_id, "type": "result", "status": "error",
                           "error": "Agent is busy (chat lock timeout)"})
            return

        # CU-105: 结构化日志上下文
        chat_t0 = time.monotonic()
        self._current_tools_called = []

        try:
            # Set current request ID for callbacks
            self._current_req_id = req_id

            # Save and restore agent's ephemeral_system_prompt around this
            # request so different skills don't leak across chat turns
            old_ephemeral = getattr(self.agent, 'ephemeral_system_prompt', None)
            if ephemeral_system_prompt:
                self.agent.ephemeral_system_prompt = ephemeral_system_prompt
                print("[Bridge] task_id={} Injected ephemeral_system_prompt ({} chars)".format(
                    req_id, len(ephemeral_system_prompt)), file=sys.stderr)

            # Build a stream callback that sends token events
            stream_enabled = params.get("stream", True)

            def _stream_callback(delta: str):
                if stream_enabled and delta:
                    _json_response({
                        "id": req_id,
                        "type": "token",
                        "content": delta,
                    })

            try:
                t0 = time.monotonic()
                response = self.agent.chat(message, stream_callback=_stream_callback if stream_enabled else None)
                t1 = time.monotonic()
                chat_ms = int((t1 - t0) * 1000)
                resp_len = len(response) if response else 0

                # CU-105: 结构化日志 — task_id/duration_ms/tools_called，不含密钥
                tools_summary = ",".join(self._current_tools_called) if self._current_tools_called else "(none)"
                print("[Bridge] task_id={} Chat completed: duration_ms={}, response_len={}, tools_called=[{}]".format(
                    req_id, chat_ms, resp_len, tools_summary), file=sys.stderr)

                _json_response({"id": req_id, "type": "result", "status": "ok",
                               "response": response or "", "duration_ms": chat_ms})
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                # CU-105: 错误也记录结构化日志
                err_ms = int((time.monotonic() - chat_t0) * 1000)
                print("[Bridge] task_id={} Chat error: duration_ms={}, error={}".format(
                    req_id, err_ms, str(e)[:200]), file=sys.stderr)
                _json_response({"id": req_id, "type": "result", "status": "error",
                               "error": str(e)})
            finally:
                # Restore agent's previous ephemeral_system_prompt
                if ephemeral_system_prompt:
                    self.agent.ephemeral_system_prompt = old_ephemeral
        finally:
            self._chat_lock.release()

    def handle_ping(self, req_id: str, params: dict):
        """Health check."""
        agent_alive = self.agent is not None
        _json_response({"id": req_id, "type": "result", "status": "ok",
                       "agent_alive": agent_alive, "model_key": self.current_model_key})

    def handle_shutdown(self, req_id: str, params: dict):
        """Graceful shutdown."""
        _json_response({"id": req_id, "type": "result", "status": "ok"})
        sys.exit(0)

    # ── AIAgent Callbacks ──────────────────────────────────────────

    def _on_tool_start(self, tool_call_id: str, tool_name: str, tool_args=None):
        """Called when a tool execution starts.

        Hermes signature: tool_start_callback(tool_call_id, name, args)
        CU-105: 记录工具名用于结构化日志汇总。
        """
        if not hasattr(self, "_tool_start_times"):
            self._tool_start_times = {}
        self._tool_start_times[tool_call_id] = time.monotonic()

        # CU-105: 追踪工具调用名
        if not hasattr(self, "_current_tools_called"):
            self._current_tools_called = []
        self._current_tools_called.append(tool_name)

        # CU-105: 结构化日志（不含工具参数，避免泄露敏感信息）
        print("[Bridge] task_id={} tool_start: name={}".format(
            self._current_req_id, tool_name), file=sys.stderr)

        _json_response({
            "id": self._current_req_id,
            "type": "tool_start",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "message": f"调用工具: {tool_name}",
        })

    def _on_tool_complete(self, tool_call_id: str, tool_name: str, tool_args=None,
                          tool_result=None):
        """Called when a tool execution completes.

        Hermes signature: tool_complete_callback(tool_call_id, name, args, result)
        Duration is measured from the matching tool_start.
        """
        duration_ms = None
        start_times = getattr(self, "_tool_start_times", None)
        if start_times is not None and tool_call_id in start_times:
            duration_ms = int((time.monotonic() - start_times.pop(tool_call_id)) * 1000)

        # 检测工具执行是否出错：result 为字符串时检查错误标记
        success = True
        if tool_result and isinstance(tool_result, str):
            err_markers = ["error:", "exception:", "traceback:", "failed:", "timeout"]
            lower_result = tool_result.lower()[:500]
            for marker in err_markers:
                if marker in lower_result:
                    success = False
                    break

        _json_response({
            "id": self._current_req_id,
            "type": "tool_end",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "message": f"工具完成: {tool_name}",
            "duration_ms": duration_ms,
            "success": success,
        })

        # CU-105: 结构化日志（不含工具结果，避免泄露敏感内容）
        print("[Bridge] task_id={} tool_end: name={}, duration_ms={}, success={}".format(
            self._current_req_id, tool_name, duration_ms, success), file=sys.stderr)

    def _on_thinking(self, content: str):
        """Called during model reasoning/thinking."""
        _json_response({
            "id": self._current_req_id,
            "type": "thinking",
            "content": content,
        })


def main():
    """Main loop: read JSON commands from stdin, dispatch to handler."""
    bridge = HermesBridge()

    handlers = {
        "init": bridge.handle_init,
        "chat": bridge.handle_chat,
        "ping": bridge.handle_ping,
        "shutdown": bridge.handle_shutdown,
    }

    print(f"[Bridge] Hermes Bridge started, pid={os.getpid()}", file=sys.stderr)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"[Bridge] Invalid JSON: {e}", file=sys.stderr)
            continue

        req_id = req.get("id", "")
        cmd = req.get("cmd", "")
        params = {k: v for k, v in req.items() if k not in ("id", "cmd")}

        handler = handlers.get(cmd)
        if handler is None:
            _json_response({"id": req_id, "type": "result", "status": "error",
                           "error": f"Unknown command: {cmd}"})
            continue

        try:
            handler(req_id, params)
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            _json_response({"id": req_id, "type": "result", "status": "error",
                           "error": str(e)})

    print("[Bridge] stdin closed, shutting down", file=sys.stderr)


if __name__ == "__main__":
    main()
