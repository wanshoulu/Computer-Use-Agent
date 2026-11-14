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

**为什么收录**：这些模块构成本工具的浏览器/桌面工具链完整闭环。收录是为了让读者
在一个仓库里看全「工具 schema → 注册 → 执行 → 安全闸门 → 后端驱动」，不必在上游
20 万行仓库里检索。**运行时推荐直接安装完整 Hermes 发行版**（官方安装脚本），
本仓库摘录仅作参考与溯源。

**衍生注意**：`tools/computer_use/cua_backend.py` 依赖上游另行安装的
[cua-driver](https://github.com/trycua/cua)（MIT）。本仓库不包含 cua-driver 本体。

## 2. 本仓库原创代码

| 目录 | 内容 | 许可 |
|---|---|---|
| `server/cua_server/routes/tasks.py` | 任务化 API：创建（SSE 事件流）/ 查询 / 订阅 / 取消 / 审计 | Apache-2.0 |
| `server/cua_server/services/task_manager.py` | 任务状态机（queued→planning→executing→waiting_user→done）、真实取消、20s ping | Apache-2.0 |
| `server/cua_server/services/computer_use_system.py` | 示教 SOP 提示词、browser 工具白名单、危险操作拦截（支付/删除/确认）、30 次调用上限 | Apache-2.0 |
| `server/cua_server/services/browser_driver.py` | CDP 确定性浏览器驱动（非 LLM 层）：健康检查/标签页/导航/截图/断线重试 | Apache-2.0 |
| `server/cua_server/services/task_policy.py` | 审计日志（AuditLogger）+ 每任务标签页绑定（TabBindingManager） | Apache-2.0 |
| `server/cua_server/services/hermes_bridge.py` | Hermes 进程桥（stdin/stdout JSON 协议） | Apache-2.0 |
| `server/cua_server/services/hermes_pool.py` | Hermes 进程池（会话隔离/空闲回收/崩溃重建） | Apache-2.0 |
| `server/cua_server/services/model_config.py` | 模型凭据解析（env / providers.json） | Apache-2.0 |
| `server/cua_server/web/index.html` | Web 控制台（聊天 + 执行时间线 + 录制流程弹窗，零依赖单文件） | Apache-2.0 |
| `demo/` | 最小 CDP 闭环冒烟（轨迹 JSON） | Apache-2.0 |
| `docs/` | 架构文档与截图 | Apache-2.0 |

**来源说明**：`server/` 下的服务端实现经过生产环境验证（含金融示教回放场景）。
其原始版本曾部署于容器镜像中，本仓库从镜像层逐字节提取后重构为独立包
`cua_server`——去除平台耦合、模型凭据改由环境变量 / `providers.json` 提供，
功能行为与生产验证版本一致。

## 3. 数据说明

`demo/` 与 `docs/` 中引用的示教回放记录（含金融示例「北向资金」）为真实运行
实录的脱敏还原，仅包含任务编排配置（示教步骤、断言、执行模型），不含任何
用户数据、凭据或隐私信息。
