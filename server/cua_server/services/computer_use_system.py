"""
Computer Use System — 提示词约束与工具集限制

P4: 限制 Computer Use Agent 的工具集，注入结构化提示词。

功能:
  - 生成 Computer Use 专用系统提示词
  - 定义允许/禁用的工具集
  - 任务约束注入（标签页绑定、操作上限）
"""

# ── 工具集配置 ──────────────────────────────────────────────────────

# Computer Use Agent 禁用的工具集（避免不必要的工具干扰）
DISABLED_TOOLSETS = ["tts", "filesystem"]

# 允许的浏览器工具白名单（用于 task_manager 约束检查）
ALLOWED_BROWSER_TOOLS = [
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_scroll",
    "browser_screenshot",
    "browser_back",
    "browser_forward",
    "browser_get_text",
    "browser_wait",
    "browser_select",
]

# 危险操作关键词（P6 task_policy 也使用）
DANGEROUS_PATTERNS = [
    "delete",
    "remove",
    "drop",
    "truncate",
    "confirm",
    "submit",
    "支付",
    "删除",
    "确认",
    "提交",
]

# 单任务最大工具调用次数
MAX_TOOL_CALLS_PER_TASK = 30


# ── 系统提示词 ──────────────────────────────────────────────────────

COMPUTER_USE_SYSTEM_PROMPT = """你是 Computer Use Agent，通过浏览器工具控制 Chrome 浏览器操作网页。

## 核心规则
1. Chrome 已通过 CDP 远程调试协议连接，无需确认连接状态，直接使用 browser_navigate 等工具操作
2. 始终先观察页面状态（截图或提取文本）再决定下一步操作
3. 遇到验证码或需要人工干预时暂停并告知用户
4. 每次操作后检查结果，确认操作是否生效
5. 不要在同一页面同时执行多个互相冲突的操作

## 操作流程
1. 理解用户意图，规划操作步骤
2. 导航到目标页面
3. 观察页面元素，定位操作目标
4. 执行操作（点击/填写/提交）
5. 验证操作结果
6. 向用户报告结果

## 约束
- 单次任务最多执行 30 次工具调用
- 涉及支付、删除、提交等敏感操作时，必须先告知用户并获得确认
- 不要在表单中自动提交涉及金融交易的表单
- 操作失败时重试不超过 3 次，然后报告错误

## 输出格式
- 操作过程使用工具调用，不需要额外解释
- 最终结果用简洁中文回复
- 如果任务未完成，说明当前状态和下一步建议
"""


def get_system_prompt(task_message: str = "") -> str:
    """获取 Computer Use Agent 系统提示词

    Args:
        task_message: 用户任务消息（可用于动态调整提示词）

    Returns:
        系统提示词字符串
    """
    return COMPUTER_USE_SYSTEM_PROMPT


def is_dangerous_action(tool_name: str, args: dict) -> bool:
    """检查工具调用是否涉及危险操作

    Args:
        tool_name: 工具名称
        args: 工具参数

    Returns:
        True 如果需要用户确认
    """
    # 检查工具名
    for pattern in DANGEROUS_PATTERNS:
        if pattern in tool_name.lower():
            return True

    # 检查参数中的 URL（可能包含 delete/confirm 等路径）
    url = args.get("url", "") or args.get("selector", "")
    for pattern in DANGEROUS_PATTERNS:
        if pattern in str(url).lower():
            return True

    return False


def should_disable_toolset(toolset_name: str) -> bool:
    """检查工具集是否应该被禁用"""
    return toolset_name in DISABLED_TOOLSETS
