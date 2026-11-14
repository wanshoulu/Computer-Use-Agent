# NOTICE — 溯源声明

本仓库包含两类代码，边界如下：

## 1. 上游代码（原样摘录，未修改）

目录：`hermes-agent/`

来源：[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
版本：`hermes_agent-0.16.0`（PyPI wheel `hermes_agent-0.16.0-py3-none-any.whl`，2026-06-26 构建）
许可：MIT License，Copyright (c) 2025 Nous Research（见 `hermes-agent/LICENSE` 原文）

收录文件（wheel 内路径原样保留）：

```text
tools/computer_use/__init__.py
tools/computer_use/backend.py
tools/computer_use/cua_backend.py
tools/computer_use/schema.py
tools/computer_use/tool.py
tools/computer_use/vision_routing.py
tools/computer_use_tool.py
tools/browser_tool.py
tools/browser_cdp_tool.py
tools/browser_supervisor.py
tools/browser_dialog_tool.py
tools/browser_camofox.py
tools/browser_camofox_state.py
tools/registry.py
tools/schema_sanitizer.py
agent/browser_provider.py
agent/browser_registry.py
hermes_cli/browser_connect.py
plugins/browser/browser_use/{__init__.py, plugin.yaml, provider.py}
plugins/browser/browserbase/{__init__.py, plugin.yaml, provider.py}
plugins/browser/firecrawl/{__init__.py, plugin.yaml, provider.py}
UPSTREAM-METADATA.txt            # wheel METADATA 原文（版本与依赖溯源）
```

**为什么收录**：这些模块构成本仓库主题（Computer Use Agent）的完整执行链路。收录
是为了让读者在一个仓库里看全「工具 schema → 注册 → 执行 → 安全闸门 → 后端驱动」
的闭环，不必在上游 20 万行仓库里检索。**如需完整发行版或最新版本，请使用上游官方
安装脚本**（见 README 快速开始）。

**衍生注意**：`tools/computer_use/cua_backend.py` 依赖上游另行安装的
[cua-driver](https://github.com/trycua/cua)（MIT）。本仓库不包含 cua-driver 本体。

## 2. 自研代码

| 目录 | 内容 | 许可 |
|---|---|---|
| `ontomind-app/apps/` | 生产 `agent_app` 表 COMPUTER_USE 记录还原（system_prompt、示教 SOP） | Apache-2.0 |
| `ontomind-app/migrations/` | `agent_app` 表 DDL 摘录与 COMPUTER_USE 种子 SQL | Apache-2.0 |
| `demo/` | 无平台依赖的 CDP 闭环 demo | Apache-2.0 |
| `docs/` | 架构文档与截图 | Apache-2.0 |
| `ontomind-app/agent-runtime/` | Computer Use Agent 服务端实现（8 文件 2,298 行：任务状态机 / CDP 驱动 / Hermes 进程桥 / 提示词约束与安全策略） | Apache-2.0 |

自研部分的事实来源：OntoMind 智能体应用平台（AI Harbor 知识建设域）
的生产 PostgreSQL 数据（`agent_app` 表，2026-11-12 快照）与其 Flyway migration
（V53/V56）。

**agent-runtime 的恢复说明**：这 8 个文件在本机工作区源码树中已缺失，本仓库从
Docker 容器镜像层（`ontomind/agent:cloud`，containerd snapshot 29731，文件时间
2026-11-04）逐字节提取恢复，与生产容器内运行的代码一致。这就是平台
「Computer Use Agent」应用（`POST /api/v1/agent-apps/app-computer-use/invoke` 与
`POST /agent/browser/tasks`）背后真正的服务端实现。

## 3. 数据说明

`ontomind-app/apps/*.json` 与 `migrations/seed_computer_use_apps.sql` 中的
system_prompt 为生产环境实录还原，仅包含应用编排配置（角色设定、示教步骤、
安全指令），不含任何用户数据、凭据或隐私信息。
