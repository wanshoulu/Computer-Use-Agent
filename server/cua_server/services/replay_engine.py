"""
Replay Engine — 确定性回放引擎（workflow-use 模式）

示教录制生成的 replay_commands 逐条确定性执行（零 LLM 调用），
单步失败时按置信度多策略自愈（browser-use 式），仍失败才让 LLM 单步兜底；
连续 3 步失败触发看门狗（强制 reload + 上下文反馈给 LLM 重规划）。

事件格式与 task_manager 一致（tool_start/tool_end/replay_step/content/done），
前端可直接复用现有渲染组件。
"""

import asyncio
import json
import time
import uuid
from typing import Optional, Dict, List, Any, AsyncGenerator

from cua_server.utils.logger import logger

# 命令解析正则（与 teach_recorder.build_replay_commands 的产物格式对应）
import re

_CMD_PATTERNS = [
    # 示教起始页可能是 http(s) 站点，也可能是 data:/file: 等本地页面
    ("open", re.compile(r"^open\s+(\S+)$")),
    ("click", re.compile(r"^click\s+(\S+)$")),
    ("fill", re.compile(r"^fill\s+(\S+)\s+(.+)$", re.S)),
    ("select", re.compile(r"^select\s+(\S+)\s+(.+)$", re.S)),
    ("press", re.compile(r"^press\s+(\S+)$")),
    ("scroll", re.compile(r"^scroll\s+(\S+)(?:\s+(\d+))?$")),
    ("snapshot", re.compile(r"^snapshot$")),
    ("back", re.compile(r"^back$")),
]


def parse_command(cmd: str) -> Optional[dict]:
    """把回放命令字符串解析为结构化动作。支持尾部 '#> hint' 元素提示。"""
    cmd = cmd.strip()
    # 剥离并提取 hint 注释
    hint = ""
    if "#>" in cmd:
        cmd, _, hint = cmd.partition("#>")
        cmd = cmd.strip()
        hint = hint.strip()

    for name, pat in _CMD_PATTERNS:
        m = pat.match(cmd)
        if m:
            return {"action": name, "args": m.groups(), "hint": hint}
    return {"action": "unknown", "args": (cmd,), "hint": hint} if cmd else None


# ---------------- 元素自愈：多策略恢复 ----------------

# 自愈统计（进程级）
_heal_stats = {"attempts": 0, "success": 0, "fail": 0, "by_strategy": {}}


def get_heal_stats() -> dict:
    return dict(_heal_stats)


async def _run_cli_async(args: list, timeout: int = 20) -> str:
    import shutil, os
    if not shutil.which("agent-browser"):
        return "error: agent-browser CLI not found"
    proc = await asyncio.create_subprocess_exec(
        "agent-browser", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=os.environ.copy(),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = (out or b"").decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            return f"error: {out[:800] or ('exit %d' % proc.returncode)}"
        return out or "(no output)"
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return f"error: timeout after {timeout}s"


async def _cli_ok(args: list, timeout: int = 20) -> bool:
    out = await _run_cli_async(args, timeout)
    return not out.startswith("error:")


async def heal_element(selector: str, hint: str = "") -> Optional[str]:
    """
    元素失效时的多策略自愈（browser-use 式，按置信度排序）：
      1. 文本内容匹配（80%）—— hint 是元素可见文本
      2. aria/placeholder/label 匹配（75%）
      3. 同标签结构位置回退（60%）
      4. snapshot 重新索引（兜底，返回 ref）
    返回恢复后的可用 selector；全部失败返回 None。
    """
    global _heal_stats
    _heal_stats["attempts"] += 1

    strategies: List[tuple] = []

    if hint:
        strategies.append(("text_match", 0.80))
    strategies.append(("attr_match", 0.75))
    strategies.append(("role_fallback", 0.60))
    strategies.append(("snapshot_ref", 0.50))

    for strat, confidence in strategies:
        try:
            # 返回值约定：'1'=找到并已标记 data-cu-heal，''=没找到
            # （避免 JS 字符串内嵌引号造成的双层转义问题）
            if strat == "text_match" and hint:
                js = (
                    'JSON.stringify((function(){'
                    ' var wanted = ' + json.dumps(hint, ensure_ascii=False) + ';'
                    ' var els = document.querySelectorAll("a,button,[role=button],input,textarea,select,[onclick]");'
                    ' for (var i=0;i<els.length;i++){'
                    '  var t=(els[i].innerText||els[i].value||"").trim();'
                    '  if(t && t.indexOf(wanted) >= 0){'
                    '   var prev=document.querySelectorAll("[data-cu-heal]");'
                    '   for(var j=0;j<prev.length;j++) prev[j].removeAttribute("data-cu-heal");'
                    '   els[i].setAttribute("data-cu-heal","1");'
                    '   return "1";'
                    '  }'
                    ' } return "";})())'
                )
                if await _eval_find(js):
                    _heal_stats["success"] += 1
                    _heal_stats["by_strategy"][strat] = _heal_stats["by_strategy"].get(strat, 0) + 1
                    return '[data-cu-heal="1"]'

            elif strat == "attr_match":
                # snapshot 索引列表里找 hint 相似的 @ref
                out = await _run_cli_async(["snapshot", "-i"], timeout=15)
                if not out.startswith("error:") and hint:
                    for line in out.split("\n"):
                        if hint[:6] in line:
                            import re as _re
                            m = _re.search(r"(@e\d+)", line)
                            if m:
                                _heal_stats["success"] += 1
                                _heal_stats["by_strategy"][strat] = _heal_stats["by_strategy"].get(strat, 0) + 1
                                return m.group(1)

            elif strat == "role_fallback":
                # 同标签兜底：页面上第一个同类元素（低置信度）
                tag = (selector or "").split("[")[0].split("#")[0].split(">")[0].split(" ")[0].strip().lower()
                if tag in ("a", "button", "input", "select", "textarea"):
                    js = (
                        'JSON.stringify((function(){'
                        ' var els = document.querySelectorAll("' + tag + '");'
                        ' if(els.length){'
                        '  var prev=document.querySelectorAll("[data-cu-heal]");'
                        '  for(var j=0;j<prev.length;j++) prev[j].removeAttribute("data-cu-heal");'
                        '  els[0].setAttribute("data-cu-heal","1");'
                        '  return "1";'
                        ' } return "";})())'
                    )
                    if await _eval_find(js):
                        _heal_stats["success"] += 1
                        _heal_stats["by_strategy"][strat] = _heal_stats["by_strategy"].get(strat, 0) + 1
                        return '[data-cu-heal="1"]'

            elif strat == "snapshot_ref":
                # 兜底：刷新 snapshot，失败时至少返回最新快照给 LLM
                out = await _run_cli_async(["snapshot", "-i"], timeout=15)
                if not out.startswith("error:"):
                    _heal_stats["fail"] += 1
                    logger.info(f"[Replay] heal exhausted for {selector}, snapshot refreshed for LLM fallback")
                    return "__NEED_LLM__"
        except Exception as e:
            logger.warning(f"[Replay] heal strategy {strat} error: {str(e)[:120]}")

    _heal_stats["fail"] += 1
    return None


async def _eval_find(js: str) -> str:
    """执行查找 JS，两层解析返回值，返回 '1'（找到）或 ''（未找到）。"""
    import base64
    b64 = base64.b64encode(js.encode("utf-8")).decode("ascii")
    out = await _run_cli_async(["eval", "-b", b64], timeout=12)
    if out.startswith("error:"):
        return ""
    try:
        data = json.loads(out)
        if isinstance(data, str):
            data = json.loads(data)
        return str(data).strip()
    except Exception:
        return ""


# ---------------- LLM 单步兜底 ----------------

async def llm_recover_step(
    pool,
    model_key: str,
    model_config: dict,
    failed_cmd: dict,
    snapshot_text: str,
) -> bool:
    """让 LLM 针对失败的单步做恢复（带页面快照上下文）。返回是否恢复成功。"""
    action = failed_cmd.get("action", "")
    args = failed_cmd.get("args", [])
    recover_prompt = (
        f"浏览器自动化任务中，这一步执行失败：\n"
        f"动作: {action} 参数: {args}\n"
        f"当前页面快照（索引元素列表）：\n{snapshot_text[:3000]}\n\n"
        f"请只做一件事：用 browser 工具完成上述动作的等价操作（点击/填写同一个目标）。"
        f"不要做其他事。完成后输出 DONE。如果目标在页面上确实不存在，输出 NOT_FOUND。"
    )
    try:
        async for event in pool.chat(
            model_key=model_key,
            model_config=model_config,
            message=recover_prompt,
            session_id="",
            ephemeral_system_prompt="你是浏览器自动化恢复助手。只完成指定的单个动作，不要多余操作。",
        ):
            if event.get("type") == "tool_end":
                if event.get("success", True):
                    return True
            elif event.get("type") == "content":
                if "NOT_FOUND" in (event.get("content") or ""):
                    return False
        return False
    except Exception as e:
        logger.warning(f"[Replay] llm_recover error: {str(e)[:150]}")
        return False


# ---------------- 回放执行器 ----------------

class ReplayRunner:
    """确定性回放执行器。逐条执行命令，失败自愈 → LLM 兜底 → 看门狗。"""

    def __init__(self, commands: List[dict], model_key: str = "", model_config: dict = None):
        self.commands = commands
        self.model_key = model_key
        self.model_config = model_config or {}
        self.consecutive_failures = 0
        self.results: List[dict] = []

    async def run(self, push_event) -> dict:
        """执行回放。push_event(dict) 用于 SSE 推送。"""
        total = len(self.commands)
        await push_event({"type": "replay_start", "total": total})

        for idx, cmd_str in enumerate(self.commands):
            parsed = parse_command(cmd_str) if isinstance(cmd_str, str) else cmd_str
            if not parsed:
                parsed = {"action": "unknown", "args": [str(cmd_str)]}

            step_id = uuid.uuid4().hex[:8]
            t0 = time.time()
            await push_event({
                "type": "replay_step",
                "step_id": step_id,
                "index": idx,
                "total": total,
                "action": parsed["action"],
                "status": "running",
                "cmd": cmd_str if isinstance(cmd_str, str) else str(cmd_str),
            })

            success, heal_info = await self._execute_step(parsed, push_event)
            duration = int((time.time() - t0) * 1000)

            if success:
                self.consecutive_failures = 0
                self.results.append({"index": idx, "ok": True, "heal": heal_info, "ms": duration})
                await push_event({
                    "type": "replay_step",
                    "step_id": step_id,
                    "index": idx,
                    "status": "success",
                    "heal": heal_info,
                    "duration_ms": duration,
                })
            else:
                self.consecutive_failures += 1
                self.results.append({"index": idx, "ok": False})
                await push_event({
                    "type": "replay_step",
                    "step_id": step_id,
                    "index": idx,
                    "status": "failed",
                    "duration_ms": duration,
                })

                # 看门狗：连续 3 步失败 → reload + LLM 重规划
                if self.consecutive_failures >= 3:
                    await push_event({"type": "replay_watchdog", "message": "连续 3 步失败，强制刷新页面并请求 LLM 重新定位"})
                    await _run_cli_async(["reload"], timeout=20)
                    await asyncio.sleep(2)
                    snap = await _run_cli_async(["snapshot", "-i"], timeout=15)
                    recovered = await llm_recover_step(
                        None, self.model_key, self.model_config,
                        parsed, snap if not snap.startswith("error:") else "",
                    ) if self.model_key else False
                    if recovered:
                        self.consecutive_failures = 0
                        await push_event({"type": "replay_watchdog", "message": "LLM 恢复成功，继续回放"})
                    else:
                        await push_event({"type": "replay_abort", "reason": f"连续失败且 LLM 无法恢复，中止于第 {idx+1}/{total} 步"})
                        break

        ok_count = sum(1 for r in self.results if r["ok"])
        summary = {
            "type": "replay_done",
            "total": total,
            "ok": ok_count,
            "failed": total - ok_count,
            "heal_stats": get_heal_stats(),
        }
        await push_event(summary)
        return summary

    async def _execute_step(self, parsed: dict, push_event) -> tuple:
        """执行单步；失败时自愈重试 → LLM 兜底。返回 (success, heal_info)。"""
        action = parsed["action"]
        args = parsed.get("args", ())
        hint = parsed.get("hint", "")

        # 首次确定性执行
        cli_args, selector, step_hint = self._to_cli_args(action, list(args))
        out = await _run_cli_async(cli_args, timeout=25)
        if not out.startswith("error:"):
            return True, None

        effective_hint = hint or step_hint

        # 失败 → 多策略自愈
        heal_sel = await heal_element(selector or "", effective_hint)
        if heal_sel and heal_sel != "__NEED_LLM__":
            cli_args2, _, _ = self._to_cli_args(action, list(args), override_selector=heal_sel)
            out2 = await _run_cli_async(cli_args2, timeout=25)
            if not out2.startswith("error:"):
                return True, f"自愈恢复（新选择器 {heal_sel}）"

        # 自愈失败 → LLM 单步兜底
        if self.model_key:
            snap = await _run_cli_async(["snapshot", "-i"], timeout=15)
            recovered = await llm_recover_step(
                None, self.model_key, self.model_config, parsed,
                snap if not snap.startswith("error:") else "",
            )
            if recovered:
                return True, "LLM 单步兜底"

        return False, None

    @staticmethod
    def _to_cli_args(action: str, args: list, override_selector: str = None) -> tuple:
        """把结构化动作转成 agent-browser CLI 参数。返回 (cli_args, selector, hint)。"""
        if action == "open":
            return (["open", args[0]], "", "")
        if action == "click":
            sel = override_selector or args[0]
            return (["click", sel], args[0], "")
        if action == "fill":
            sel = override_selector or args[0]
            val = args[1].strip('"') if len(args) > 1 else ""
            return (["fill", sel, val], args[0], val)
        if action == "select":
            sel = override_selector or args[0]
            val = args[1].strip('"') if len(args) > 1 else ""
            return (["select", sel, val], args[0], val)
        if action == "press":
            return (["press", args[0]], "", "")
        if action == "scroll":
            cli = ["scroll", args[0]]
            if len(args) > 1 and args[1]:
                cli.append(str(args[1]))
            return (cli, "", "")
        if action == "snapshot":
            return (["snapshot", "-i"], "", "")
        if action == "back":
            return (["back"], "", "")
        return (["snapshot", "-i"], "", "")


# ---------------- 会话式回放入口（供路由调用） ----------------

async def run_replay(
    commands: List[str],
    model_key: str = "",
    model_config: dict = None,
) -> AsyncGenerator[dict, None]:
    """回放入口：AsyncGenerator 事件流，与 task_manager 事件格式兼容。"""
    queue: asyncio.Queue = asyncio.Queue()

    async def push_event(ev: dict):
        await queue.put(ev)

    runner = ReplayRunner(commands, model_key, model_config)

    async def _run():
        try:
            await runner.run(push_event)
        except Exception as e:
            await queue.put({"type": "error", "error": str(e)[:300]})
        finally:
            await queue.put(None)

    task = asyncio.create_task(_run())
    while True:
        ev = await queue.get()
        if ev is None:
            break
        yield ev
