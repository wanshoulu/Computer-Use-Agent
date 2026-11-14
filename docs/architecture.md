# Computer Use Agent — 架构与设计决策

## 1. 它是什么

Computer Use Agent = 「可访问性树优先的网页/桌面自动化智能体」。核心循环：

```text
browser_snapshot (文本快照) → 模型决策 → browser_click/type/eval → 结果回灌 → 再 snapshot
```

本仓库主题来自 OntoMind 平台生产环境的 `COMPUTER_USE` 类型 AgentApp（应用管理页
可见的「Computer Use Agent」卡片），执行器为 Hermes Agent 的 browser/computer_use
工具集，浏览器经 Chrome DevTools Protocol（CDP）接入。

## 2. 名词表

| 名词 | 解释 | 来源 |
|---|---|---|
| Computer Use | 让模型通过截图/可访问性树观察屏幕并合成鼠标键盘事件来操作 GUI 的能力类别 | [Anthropic computer use](https://docs.claude.com/en/docs/agents-and-tools/computer-use)、[Hermes computer-use 文档](https://github.com/ocuclaw/hermes-agent/blob/main/website/docs/user-guide/features/computer-use.md) |
| CDP | Chrome DevTools Protocol，Chrome 的远程调试协议（WebSocket），可下发截图/DOM/输入/网络等任意 DevTools 命令 | [chromedevtools.github.io/devtools-protocol](https://chromedevtools.github.io/devtools-protocol/) |
| ariaSnapshot | Playwright 风格的页面可访问性树文本快照，元素以 `@eN` 引用 | Hermes browser_tool（agent-browser CLI） |
| SOM | Set-of-Mark：截图上给每个可交互元素画编号覆盖层，模型按编号点击而非算像素坐标 | Hermes computer_use |
| cua-driver | 开源后台桌面操控驱动（macOS SkyLight SPI / Windows UIA+SendInput / Linux AT-SPI+XTest），不抢用户光标与焦点 | [trycua/cua](https://github.com/trycua/cua) |
| MCP | Model Context Protocol，工具与 Agent 间的标准协议；cua-driver 以 MCP stdio 方式接入 |上游 schema 注释 |
| 示教回放 | 用户示教一遍操作步骤（记录选择器序列），智能体按 SOP 顺序回放 | 本仓库生产应用记录 |

## 3. 设计决策与代价

### 3.1 可访问性树优先，视觉其次（而非相反）

**决策**：网页任务的每一步先 `browser_snapshot`（ariaSnapshot 文本），模型按
`@eN` 引用交互；截图（`browser_vision` / SOM）作为补充能力按需调用。

**依据**：生产 system_prompt 原文「始终先调用 browser_snapshot 观察页面状态再决定
下一步操作」；上游 computer_use 文档「Vision models get SOM captures … so they
click by element index instead of pixel coordinates」。

**代价**：
- 依赖页面可访问性树质量 —— 自绘 canvas / 无语义 DOM 的页面（游戏、某些设计工具）快照贫瘠，必须回退视觉路线
- 多一次往返（snapshot → 决策），纯视觉路线可一步出坐标

**收益**：无视觉能力的模型（纯文本 LLM）同样可驱动；点击不因分辨率缩放/布局微调漂移；token 消耗远低于每轮传截图。

### 3.2 双工具面而非单一 computer_use

**决策**：网页走 `browser_*`（独立 headless Chromium），原生桌面走 `computer_use`
（cua-driver）。上游文档同样明确建议 web 任务优先 browser 工具。

**代价**：两套后端、两套会话管理（browser_supervisor 1,475 行 + cua MCP 生命周期）。

**收益**：headless Chromium 无需桌面权限、可并发、更可靠；cua-driver 的「无前台合约」（不动用户光标/焦点/Space）让 agent 与用户可同机协作。

### 3.3 应用编排放 system_prompt，不放代码

**决策**：OntoMind 的 AgentApp 把「角色设定 + 示教 SOP + 安全边界」全部写在
`agent_app.system_prompt`（TEXT 字段），执行器零改动。

**代价**：prompt 是运行时行为的一部分，版本化只能靠 `agent_app.version` 字段与快照表（`application_knowledge_manifest`），没有代码级 diff。

**收益**：改行为不用发版；示教回放（用户口述步骤 → 选择器序列）可以由运营直接配置；同一执行器可服务 N 个不同 SOP 的应用（生产表里还有示教回放应用、闭环测试应用）。

### 3.4 CDP 透传作为逃生舱

**决策**：保留 `browser_cdp`（任意 DevTools 命令）与 `browser_eval`（页面内 JS 求值）作为兜底；生产 prompt 明示「常规点击不生效时可用 browser_eval 提交表单」。

**代价**：eval 等于在页面上下文执行任意 JS，权限面大。

**收益**：覆盖 shadow DOM 命中失败、原生弹窗、iframe 隔离等 ariaSnapshot 覆盖不到的场景。缓解措施：URL 安全检查 + 平台侧授权 + prompt 级「遇验证码/人工干预暂停」。

## 4. 执行链路（关键文件索引）

```text
模型 tool_call
  → tools/registry.py（注册中心，schema 校验）
  → tools/browser_tool.py（browser_* 分发；本地 Chromium / 云后端自动探测）
      → tools/browser_supervisor.py（进程生命周期、会话隔离）
      → agent-browser CLI（ariaSnapshot / 元素交互 / 截图）
      → Chrome CDP（WebSocket）
  或 → tools/computer_use/tool.py（审批闸门 → 动作分发）
      → tools/computer_use/cua_backend.py（MCP stdio → cua-driver）
  或 → tools/browser_cdp_tool.py（CDP 透传）
```

## 5. 已知限制（诚实列出）

1. **上游代码未升级**：摘录自 v0.16.0（2026-06）。上游已发布跨平台 computer_use（Windows/Linux）等新特性，本仓库摘录版本的 `cua_backend.py` 文档注释仍标 macOS-only；生产环境实际通过 `browser_*` 路线在 Windows 上运行，未验证 computer_use 的 Windows 后端。
2. **摘录非完整发行版**：`hermes-agent/` 仅收录 Computer Use/Browser 链路相关文件，缺 agent loop、provider 适配等外围模块，不能直接 `pip install` 运行 —— 请用上游官方安装脚本装完整 Hermes，本仓库的价值在「链路讲解 + 生产编排资产 + demo」。
3. **Server 端 invoke 语义有限**：OntoMind `KnowledgeQaExecutionService.invoke` 目前面向 KNOWLEDGE_QA 语义（知识库检索 + 问答）；COMPUTER_USE 应用的实际执行走 Hermes 运行时，`POST /agent-apps/{id}/invoke` 的 COMPUTER_USE 分支在平台 server 侧未见专属实现（生产通过配置驱动）。复刻平台形态时需要自行接执行器。
4. **闭环 demo 的 browser 依赖**：demo 需要本机 Chrome 且以 `--remote-debugging-port` 启动；无头 Linux 环境建议改用上游 `agent-browser install` 的托管 Chromium。
5. **验证码**：生产策略是「检测到即暂停并告知用户」，未集成打码/人工接管通道。
6. **数据时效**：`agent_app` 记录还原自 2026-11-12 的 PostgreSQL 快照，之后平台上的修改不反映在本仓库。

## 6. 与同类实现的对比

| 维度 | 本方案（Hermes browser_*） | Anthropic computer_20251124 | 纯视觉 CUA（pyautogui 系） |
|---|---|---|---|
| 观察介质 | ariaSnapshot 文本 + 可选截图 | 截图（强制视觉） | 截图 |
| 点击定位 | `@eN` 元素引用 | 像素坐标 | 像素坐标 |
| 非视觉模型 | ✅ 可用 | ❌ 不可用 | ❌ 不可用 |
| Schema | 通用 OpenAI function-calling | Anthropic 专有 tool 类型 | 自定义 |
| 桌面后台操控 | cua-driver（不抢焦点） | — | 通常抢焦点 |
| 逃生舱 | browser_cdp / browser_eval | — | — |
