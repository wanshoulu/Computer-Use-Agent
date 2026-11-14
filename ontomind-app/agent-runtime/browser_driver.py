"""
Browser Driver — CDP 确定性浏览器操作层

P3: 封装 Chrome DevTools Protocol，提供确定性浏览器操作（非 LLM 驱动）。

功能:
  - 健康检查（HTTP /json/list）
  - 标签页管理（创建/关闭/列表）
  - 页面操作（导航/点击/填写/提取文本）
  - 全页截图（CDP Page.captureScreenshot）
  - 断线恢复（3s 重试）
"""
import asyncio
import json
import os
import re
import time
import uuid
from typing import Optional, Dict, List, Any

import httpx

from src.ontomind_agent.utils.logger import logger


def _get_cdp_http_base() -> str:
    """从 BROWSER_CDP_URL 解析 CDP HTTP 端点"""
    raw = os.environ.get("BROWSER_CDP_URL", "").strip()
    if not raw:
        return ""
    m = re.match(r"^(https?://[^/]+)", raw)
    return m.group(1) if m else ""


def _fix_ws_url(ws_url: str, cdp_http: str) -> str:
    """替换 webSocketDebuggerUrl 中的 localhost 为实际 CDP host"""
    if cdp_http:
        cdp_host_port = cdp_http.replace("http://", "").replace("https://", "").rstrip("/")
        ws_url = ws_url.replace("localhost:9222", cdp_host_port)
        ws_url = ws_url.replace("127.0.0.1:9222", cdp_host_port)
    return ws_url


class BrowserDriver:
    """CDP 浏览器驱动 — 确定性操作层

    使用 Chrome DevTools Protocol 通过 WebSocket 直接控制浏览器，
    不依赖 LLM，提供可预测的页面操作。
    """

    def __init__(self):
        self._cdp_http = _get_cdp_http_base()
        self._ws: Optional[Any] = None
        self._msg_id = 0
        self._target_id: Optional[str] = None

    async def health_check(self) -> dict:
        """检查 Chrome CDP 是否可连接

        Returns:
            {"ok": bool, "targets": int, "pages": int, "cdp_url": str}
        """
        if not self._cdp_http:
            return {"ok": False, "error": "BROWSER_CDP_URL 未设置", "cdp_url": ""}

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._cdp_http}/json/list")
                if resp.status_code != 200:
                    return {"ok": False, "error": f"HTTP {resp.status_code}", "cdp_url": self._cdp_http}
                targets = resp.json()
                pages = [t for t in targets if t.get("type") == "page"]
                return {
                    "ok": True,
                    "targets": len(targets),
                    "pages": len(pages),
                    "cdp_url": self._cdp_http,
                }
        except Exception as e:
            return {"ok": False, "error": str(e), "cdp_url": self._cdp_http}

    async def list_pages(self) -> List[dict]:
        """列出所有 page 类型的标签页"""
        if not self._cdp_http:
            return []
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._cdp_http}/json/list")
                targets = resp.json()
                return [
                    {
                        "id": t.get("id", ""),
                        "title": t.get("title", ""),
                        "url": t.get("url", ""),
                        "ws_url": t.get("webSocketDebuggerUrl", ""),
                    }
                    for t in targets
                    if t.get("type") == "page"
                ]
        except Exception as e:
            logger.error(f"[BrowserDriver] list_pages failed: {e}")
            return []

    async def create_tab(self, url: str = "about:blank") -> Optional[dict]:
        """创建新标签页

        Returns:
            {"id": "...", "url": "...", "ws_url": "..."} 或 None
        """
        if not self._cdp_http:
            return None
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.put(f"{self._cdp_http}/json/new?{url}")
                if resp.status_code != 200:
                    logger.error(f"[BrowserDriver] create_tab failed: HTTP {resp.status_code}")
                    return None
                target = resp.json()
                return {
                    "id": target.get("id", ""),
                    "url": target.get("url", url),
                    "ws_url": target.get("webSocketDebuggerUrl", ""),
                }
        except Exception as e:
            logger.error(f"[BrowserDriver] create_tab error: {e}")
            return None

    async def close_tab(self, target_id: str) -> bool:
        """关闭指定标签页"""
        if not self._cdp_http:
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._cdp_http}/json/close/{target_id}")
                return resp.status_code == 200
        except Exception as e:
            logger.error(f"[BrowserDriver] close_tab error: {e}")
            return False

    async def navigate(self, url: str, target_id: Optional[str] = None) -> dict:
        """导航到指定 URL

        Args:
            url: 目标 URL
            target_id: 可选标签页 ID，不指定则使用当前活跃页

        Returns:
            {"ok": bool, "url": str, "error": str}
        """
        target = await self._get_or_pick_target(target_id)
        if not target:
            return {"ok": False, "error": "没有可用的标签页"}

        ws_url = _fix_ws_url(target.get("ws_url", ""), self._cdp_http)
        if not ws_url:
            return {"ok": False, "error": "缺少 webSocketDebuggerUrl"}

        try:
            import websockets
            async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
                # 导航
                result = await self._cdp_send(ws, "Page.navigate", {"url": url})
                if "error" in result:
                    return {"ok": False, "error": result["error"].get("message", "Navigate failed")}
                # 等待页面加载
                await asyncio.sleep(1)
                return {"ok": True, "url": url}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def click_element(self, selector: str, target_id: Optional[str] = None) -> dict:
        """点击指定 CSS 选择器的元素

        Returns:
            {"ok": bool, "clicked": bool, "error": str}
        """
        target = await self._get_or_pick_target(target_id)
        if not target:
            return {"ok": False, "error": "没有可用的标签页"}

        ws_url = _fix_ws_url(target.get("ws_url", ""), self._cdp_http)
        try:
            import websockets
            async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
                # 查找元素
                expr = f'document.querySelector("{selector}")'
                result = await self._cdp_send(ws, "Runtime.evaluate", {
                    "expression": expr,
                    "returnByValue": True,
                })
                remote_obj = result.get("result", {}).get("result", {})
                if not remote_obj or remote_obj.get("type") == "undefined":
                    return {"ok": False, "clicked": False, "error": f"元素不存在: {selector}"}

                # 点击
                click_expr = f'document.querySelector("{selector}").click()'
                await self._cdp_send(ws, "Runtime.evaluate", {
                    "expression": click_expr,
                })
                await asyncio.sleep(0.3)
                return {"ok": True, "clicked": True}
        except Exception as e:
            return {"ok": False, "clicked": False, "error": str(e)}

    async def fill_input(self, selector: str, value: str, target_id: Optional[str] = None) -> dict:
        """填写输入框

        Returns:
            {"ok": bool, "filled": bool, "error": str}
        """
        target = await self._get_or_pick_target(target_id)
        if not target:
            return {"ok": False, "error": "没有可用的标签页"}

        ws_url = _fix_ws_url(target.get("ws_url", ""), self._cdp_http)
        try:
            import websockets
            async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
                expr = f'document.querySelector("{selector}").value = "{value}"'
                result = await self._cdp_send(ws, "Runtime.evaluate", {
                    "expression": expr,
                    "returnByValue": True,
                })
                if "error" in result:
                    return {"ok": False, "filled": False, "error": result["error"].get("message", "")}
                return {"ok": True, "filled": True}
        except Exception as e:
            return {"ok": False, "filled": False, "error": str(e)}

    async def extract_text(self, selector: Optional[str] = None,
                           target_id: Optional[str] = None) -> dict:
        """提取页面文本

        Args:
            selector: CSS 选择器，不指定则提取 body 文本

        Returns:
            {"ok": bool, "text": str, "error": str}
        """
        target = await self._get_or_pick_target(target_id)
        if not target:
            return {"ok": False, "error": "没有可用的标签页"}

        ws_url = _fix_ws_url(target.get("ws_url", ""), self._cdp_http)
        try:
            import websockets
            async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
                sel = selector or "body"
                expr = f'document.querySelector("{sel}")?.innerText || ""'
                result = await self._cdp_send(ws, "Runtime.evaluate", {
                    "expression": expr,
                    "returnByValue": True,
                })
                text = result.get("result", {}).get("result", {}).get("value", "")
                return {"ok": True, "text": text}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def screenshot(self, target_id: Optional[str] = None,
                         full_page: bool = True) -> dict:
        """全页截图

        Returns:
            {"ok": bool, "data": str(base64), "error": str}
        """
        target = await self._get_or_pick_target(target_id)
        if not target:
            return {"ok": False, "error": "没有可用的标签页"}

        ws_url = _fix_ws_url(target.get("ws_url", ""), self._cdp_http)
        try:
            import websockets
            async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
                params = {"format": "png"}
                if full_page:
                    params["captureBeyondViewport"] = True
                result = await self._cdp_send(ws, "Page.captureScreenshot", params)
                data = result.get("result", {}).get("data", "")
                if not data:
                    return {"ok": False, "error": "截图数据为空"}
                return {"ok": True, "data": data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── 内部方法 ────────────────────────────────────────────────────

    async def _get_or_pick_target(self, target_id: Optional[str] = None) -> Optional[dict]:
        """获取指定 target 或自动选取"""
        pages = await self.list_pages()
        if not pages:
            return None
        if target_id:
            for p in pages:
                if p["id"] == target_id:
                    return p
            return None
        # 优先选非空白页
        for p in pages:
            url = p.get("url", "")
            if url and not url.startswith("about:blank") and not url.startswith("chrome://"):
                return p
        return pages[0]

    async def _cdp_send(self, ws, method: str, params: dict = None) -> dict:
        """发送 CDP 命令并等待对应 id 的响应

        单次 WebSocket 连接内同步发送+接收，不支持并发（每次操作独立连接）。
        """
        self._msg_id += 1
        msg_id = self._msg_id
        cmd = {"id": msg_id, "method": method, "params": params or {}}
        await ws.send(json.dumps(cmd))

        # 读响应，匹配 msg_id
        timeout = 10
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
                data = json.loads(raw)
                if data.get("id") == msg_id:
                    return data
                # 其他消息（事件）忽略
            except asyncio.TimeoutError:
                return {"error": {"message": "CDP response timeout"}}
        return {"error": {"message": "CDP response timeout"}}


# 全局单例
_browser_driver: Optional[BrowserDriver] = None


def get_browser_driver() -> BrowserDriver:
    global _browser_driver
    if _browser_driver is None:
        _browser_driver = BrowserDriver()
    return _browser_driver
