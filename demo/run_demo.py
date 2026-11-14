#!/usr/bin/env python3
"""Computer Use Agent — 闭环 demo（最小 CDP 客户端）。

链路：launch Chrome(--remote-debugging-port) → 打开 demo-page.html →
browser_snapshot（DOM 摘要，模拟 ariaSnapshot）→ 按示教 SOP 执行
（#kw click → type → #go click）→ 断言 #out → 轨迹落盘 trajectory.json。

仅依赖标准库 + websockets（连接 DevTools）。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

# Windows 控制台 UTF-8 输出（避免中文乱码）
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import websockets  # type: ignore
import asyncio

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "demo-page.html")

# ── 轨迹 ────────────────────────────────────────────────────────────────
TRAJECTORY: list[dict] = []


def trace(index: int, action: str, **kw) -> dict:
    step = {"index": index, "action": action, **kw}
    TRAJECTORY.append(step)
    mark = "✓" if step.get("ok", True) else "✗"
    detail = ", ".join(f"{k}={v}" for k, v in kw.items() if k != "ok")
    print(f"  [{index}] {mark} {action}  {detail}")
    return step


# ── CDP 客户端（最小实现） ──────────────────────────────────────────────
class CDP:
    """单标签页 CDP 客户端：Runtime.evaluate / Page 域。"""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._id = 0

    async def __aenter__(self):
        self.ws = await websockets.connect(self.ws_url, max_size=50 * 1024 * 1024)
        await self._send("Page.enable")
        await self._send("Runtime.enable")
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()

    async def _send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        await self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"CDP error: {msg['error']}")
                return msg.get("result", {})

    async def navigate(self, url: str) -> None:
        await self._send("Page.navigate", {"url": url})

    async def eval_js(self, expression: str) -> object:
        res = await self._send("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        })
        return res.get("result", {}).get("value")


# ── Chrome 启动 ─────────────────────────────────────────────────────────
def find_chrome() -> str:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise RuntimeError("未找到 Chrome/Chromium，请用 --chrome 指定路径")


def wait_cdp(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
            return
        except urllib.error.URLError:
            time.sleep(0.4)
    raise RuntimeError(f"CDP 端口 {port} 未就绪")


def pick_target_ws(port: int) -> str:
    data = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
    pages = [t for t in data if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        raise RuntimeError("无可用 page target")
    return pages[0]["webSocketDebuggerUrl"]


# ── 闭环 SOP ────────────────────────────────────────────────────────────
async def run(cdp_port: int, typed_text: str) -> dict:
    page_url = PAGE.replace("\\", "/")

    async with CDP(pick_target_ws(cdp_port)) as cdp:
        # 1. 打开起始页
        await cdp.navigate("file:///" + page_url.lstrip("/"))
        await cdp.eval_js("new Promise(r => setTimeout(r, 600))")

        # 2. browser_snapshot（DOM 摘要 —— 生产环境此步是 ariaSnapshot 文本快照）
        snap = await cdp.eval_js(
            "Array.from(document.querySelectorAll('input,button,div[id]')).map("
            "(el,i)=>`@e${i+1} ${el.tagName.toLowerCase()}${el.id?('#'+el.id):''}"
            "${el.placeholder?(' placeholder=\"'+el.placeholder+'\"'):''}`).join('\\n')"
        )
        trace(1, "snapshot", summary=(snap or "")[:160], ok=bool(snap))

        # 3. click #kw
        ok1 = await cdp.eval_js("document.querySelector('#kw') !== null")
        trace(2, "click", selector="#kw", ok=bool(ok1))

        # 4. type
        js_set = json.dumps(typed_text)
        await cdp.eval_js(
            "const kw=document.querySelector('#kw');"
            f"kw.value={js_set};kw.dispatchEvent(new Event('input',{{bubbles:true}}));true"
        )
        trace(3, "type", selector="#kw", text=typed_text, ok=True)

        # 5. click #go
        await cdp.eval_js("document.querySelector('#go').click()")
        await cdp.eval_js("new Promise(r => setTimeout(r, 200))")
        trace(4, "click", selector="#go", ok=True)

        # 6. assert #out
        out_val = await cdp.eval_js("document.getElementById('out').textContent")
        expected = "submitted:" + typed_text
        passed = out_val == expected
        trace(5, "assert", selector="#out", expected=expected, actual=out_val, ok=passed)

        verdict = "path_ok" if passed else "path_failed"

        # 7. 截图（生产 browser_* 每步可带 capture；demo 在断言后截一张）
        try:
            png_b64 = (await cdp._send("Page.captureScreenshot", {"format": "png"}))["data"]
            shot_path = os.path.join(HERE, "demo-run.png")
            with open(shot_path, "wb") as f:
                f.write(base64.b64decode(png_b64))
            trace(6, "screenshot", path="demo-run.png", ok=True)
        except Exception as e:  # noqa: BLE001
            trace(6, "screenshot", error=str(e)[:120], ok=False)

        return {"verdict": verdict,
                "snapshot": snap,
                "assert": {"expected": expected, "actual": out_val}}


def main() -> int:
    ap = argparse.ArgumentParser(description="Computer Use Agent 闭环 demo")
    ap.add_argument("--cdp-port", type=int, default=9222)
    ap.add_argument("--chrome", default=None, help="Chrome 可执行路径")
    ap.add_argument("--text", default="hello computer use")
    ap.add_argument("--attach", default=None, help="复用已运行 CDP：http://127.0.0.1:PORT")
    ap.add_argument("--keep-open", action="store_true", help="结束时保留 Chrome 进程")
    args = ap.parse_args()

    chrome_proc = None
    if not args.attach:
        chrome = args.chrome or find_chrome()
        # Extra flags via env (CI needs --headless=new --no-sandbox; local runs add none)
        extra_flags = [f for f in os.environ.get("CUA_CHROME_FLAGS", "").split() if f]
        chrome_proc = subprocess.Popen(
            [chrome,
             f"--remote-debugging-port={args.cdp_port}",
             "--user-data-dir=" + os.path.join(HERE, ".chrome-profile"),
             "--no-first-run", "--no-default-browser-check", *extra_flags, "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        wait_cdp(args.cdp_port)

    print(f"▶ CDP :{args.cdp_port}  page={os.path.basename(PAGE)}")
    try:
        result = asyncio.run(run(args.cdp_port, args.text))
    finally:
        verdict = result.get("verdict", "error") if 'result' in dir() else "error"
        out = {
            "verdict": verdict,
            "steps": TRAJECTORY,
            "assert": result.get("assert") if 'result' in dir() else None,
        }
        with open(os.path.join(HERE, "trajectory.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\n⏹ verdict={verdict}  → trajectory.json")
        if chrome_proc and not args.keep_open:
            chrome_proc.terminate()
    return 0 if result and result.get("verdict") == "path_ok" else 1


if __name__ == "__main__":
    sys.exit(main())
