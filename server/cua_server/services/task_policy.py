"""
Task Policy — 安全策略层

P6: 危险操作确认、敏感信息脱敏、审计日志、标签页绑定。

功能:
  - check_action() — 检查工具调用是否需要用户确认
  - sanitize_log() — 脱敏日志中的 API key/token/密码
  - audit_log() — 记录操作审计日志
  - TabBinding — 任务与标签页绑定，防止跨任务干扰
"""
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, List

from cua_server.services.computer_use_system import is_dangerous_action, DANGEROUS_PATTERNS
from cua_server.utils.logger import logger


# ── 敏感信息脱敏 ────────────────────────────────────────────────────

# 需要脱敏的模式列表
_SENSITIVE_PATTERNS = [
    # API keys (sk-xxx, key-xxx)
    (re.compile(r'(sk-|key-)[a-zA-Z0-9]{20,}'), r'\1****REDACTED'),
    # Bearer tokens
    (re.compile(r'(Bearer\s+)[a-zA-Z0-9._-]{20,}', re.IGNORECASE), r'\1****REDACTED'),
    # Password fields in JSON
    (re.compile(r'("password"\s*:\s*")[^"]+(")'), r'\1****REDACTED\2'),
    # api_key in JSON
    (re.compile(r'("api_key"\s*:\s*")[^"]+(")'), r'\1****REDACTED\2'),
    # token in JSON
    (re.compile(r'("token"\s*:\s*")[^"]+(")'), r'\1****REDACTED\2'),
    # Authorization header
    (re.compile(r'(Authorization:\s*)[a-zA-Z0-9._-]+', re.IGNORECASE), r'\1****REDACTED'),
    # Email addresses (partial mask)
    (re.compile(r'([a-zA-Z0-9._%+-]{2})[a-zA-Z0-9._%+-]*@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})'), r'\1***@\2'),
]


def sanitize_log(text: str) -> str:
    """脱敏日志文本中的敏感信息

    Args:
        text: 原始日志文本

    Returns:
        脱敏后的文本
    """
    if not text:
        return text
    result = text
    for pattern, replacement in _SENSITIVE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


# ── 审计日志 ────────────────────────────────────────────────────────

@dataclass
class AuditEntry:
    """审计日志条目"""
    timestamp: str
    task_id: str
    action: str  # tool_start / tool_end / navigate / click / cancel
    tool_name: str = ""
    params_summary: str = ""  # 脱敏后的参数摘要
    result_status: str = ""  # success / error / cancelled
    duration_ms: Optional[int] = None
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "task_id": self.task_id,
            "action": self.action,
            "tool_name": self.tool_name,
            "params_summary": self.params_summary,
            "result_status": self.result_status,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


class AuditLogger:
    """审计日志记录器 — 内存存储，应用重启后清空"""

    def __init__(self, max_entries: int = 1000):
        self._entries: List[AuditEntry] = []
        self._max_entries = max_entries

    def log(self, task_id: str, action: str, tool_name: str = "",
             params: dict = None, result_status: str = "", 
             duration_ms: int = None, error: str = ""):
        """记录审计日志

        Args:
            task_id: 任务 ID
            action: 操作类型
            tool_name: 工具名
            params: 工具参数（会被脱敏）
            result_status: 结果状态
            duration_ms: 耗时
            error: 错误信息
        """
        # 脱敏参数
        params_str = sanitize_log(json.dumps(params, ensure_ascii=False)) if params else ""
        # 截断过长的参数
        if len(params_str) > 200:
            params_str = params_str[:200] + "..."

        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            task_id=task_id,
            action=action,
            tool_name=tool_name,
            params_summary=params_str,
            result_status=result_status,
            duration_ms=duration_ms,
            error=sanitize_log(error)[:200] if error else "",
        )
        self._entries.append(entry)
        if len(self._entries) > self._max_entries:
            self._entries.pop(0)

        # 同时输出到结构化日志
        logger.info(
            "[Audit] task_id={} action={} tool={} status={} duration={}ms".format(
                task_id, action, tool_name, result_status, duration_ms or 0
            )
        )

    def get_entries(self, task_id: Optional[str] = None, limit: int = 100) -> List[dict]:
        """获取审计日志"""
        entries = self._entries
        if task_id:
            entries = [e for e in entries if e.task_id == task_id]
        return [e.to_dict() for e in entries[-limit:]]


# ── 标签页绑定 ──────────────────────────────────────────────────────

@dataclass
class TabBinding:
    """任务与浏览器标签页的绑定关系

    每个任务可以绑定一个独立的标签页，防止多任务操作同一页面互相干扰。
    """
    task_id: str
    tab_id: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    url: str = ""
    title: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "tab_id": self.tab_id,
            "created_at": self.created_at,
            "url": self.url,
            "title": self.title,
        }


class TabBindingManager:
    """标签页绑定管理器"""

    def __init__(self):
        self._bindings: Dict[str, TabBinding] = {}  # task_id → TabBinding

    async def bind(self, task_id: str, url: str = "about:blank") -> Optional[TabBinding]:
        """为任务创建并绑定一个新标签页

        Args:
            task_id: 任务 ID
            url: 初始 URL

        Returns:
            TabBinding 或 None（创建失败时）
        """
        from cua_server.services.browser_driver import get_browser_driver
        driver = get_browser_driver()

        tab = await driver.create_tab(url)
        if tab is None:
            logger.warning(f"[TabBinding] Failed to create tab for task {task_id}")
            return None

        binding = TabBinding(
            task_id=task_id,
            tab_id=tab["id"],
            url=tab.get("url", url),
        )
        self._bindings[task_id] = binding
        logger.info(f"[TabBinding] Task {task_id} bound to tab {tab['id']}")
        return binding

    def get_binding(self, task_id: str) -> Optional[TabBinding]:
        """获取任务的标签页绑定"""
        return self._bindings.get(task_id)

    async def unbind(self, task_id: str) -> bool:
        """解除任务标签页绑定（关闭标签页）

        Returns:
            True 如果成功关闭
        """
        binding = self._bindings.get(task_id)
        if binding is None:
            return False

        from cua_server.services.browser_driver import get_browser_driver
        driver = get_browser_driver()

        success = await driver.close_tab(binding.tab_id)
        del self._bindings[task_id]
        logger.info(f"[TabBinding] Task {task_id} unbound from tab {binding.tab_id} (closed={success})")
        return success


# ── 全局单例 ────────────────────────────────────────────────────────

_audit_logger: Optional[AuditLogger] = None
_tab_binding_manager: Optional[TabBindingManager] = None


def get_audit_logger() -> AuditLogger:
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger


def get_tab_binding_manager() -> TabBindingManager:
    global _tab_binding_manager
    if _tab_binding_manager is None:
        _tab_binding_manager = TabBindingManager()
    return _tab_binding_manager
