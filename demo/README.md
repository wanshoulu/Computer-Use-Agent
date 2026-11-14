# 闭环 Demo

不依赖后端服务，用一个内置测试页验证 CDP 闭环的最小形态（完整服务端见 `server/`）：

```text
启动 Chrome（CDP）→ 打开测试页 → browser_snapshot → 按 SOP 交互 → 断言 → 输出轨迹
```

## 测试页

`demo-page.html` 是从生产 `agent_app` 表「闭环测试-1794058473」记录还原的起始页
（data: URL → 本地文件）：

- 输入框 `#kw`（placeholder「搜索关键词」）
- 按钮 `#go`：点击后把输入回显到 `#out`（`submitted:<内容>`）

生产 SOP（示教步骤）：`1. 点击 #kw  2. 输入文本  3. 点击 #go`，断言 `#out` 文本。

## 运行

```bash
pip install -r requirements.txt          # websockets, requests
python run_demo.py                       # 自动启动 Chrome + CDP 并跑闭环
python run_demo.py --cdp-port 9223       # 指定 CDP 端口
python run_demo.py --attach cdp-url      # 连接已运行的 Chrome（先手动开 --remote-debugging-port）
```

## 轨迹输出

运行结束打印并在 `trajectory.json` 落盘每一步：

```json
{
  "steps": [
    {"index": 1, "action": "snapshot",  "summary": "@e1 input#kw, @e2 button#go, @e3 div#out"},
    {"index": 2, "action": "click",     "selector": "#kw",  "ok": true},
    {"index": 3, "action": "type",      "selector": "#kw",  "text": "...", "ok": true},
    {"index": 4, "action": "click",     "selector": "#go",  "ok": true},
    {"index": 5, "action": "assert",    "selector": "#out", "expected": "submitted:...", "ok": true}
  ],
  "verdict": "path_ok",
  "elapsed_ms": 1834
}
```

与生产 `flywheel`/闭环测试的轨迹 schema 同构（steps + verdict + elapsed），便于对接归因分析。

## 边界说明

- 本 demo 的执行器是**最小 CDP 客户端**（约 300 行：launch Chrome → WebSocket 连 DevTools → Runtime.evaluate/Page.captureSnapshot），用于演示闭环与轨迹格式；完整工具面（ariaSnapshot、元素引用、云后端、审批闸门）见 `../hermes-agent/tools/browser_tool.py`。
- 不含 LLM 调用：SOP 步骤硬编码，聚焦「CDP 闭环 + 轨迹落盘」；要接模型请用上游 Hermes 完整安装（README 方式二）。
