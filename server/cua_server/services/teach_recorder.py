"""
Teach Recorder — 示教录制服务（Computer Use 应用"用户演示一遍→固化应用"能力）

原理（跨导航不丢事件的可靠方案）：
1. 通过 agent-browser addinitscript 注册注入脚本（CDP Page.addScriptToEvaluateOnNewDocument）
   —— 每次页面导航后自动执行，钩子永不丢失
2. 注入脚本捕获用户的 click / fill / select / Enter 事件
3. 每个事件即时通过 navigator.sendBeacon POST 到本服务 /agent/browser/record/event
   —— sendBeacon 即使页面立即导航也能送达；跨 origin 免 CORS（beacon 规范豁免）
4. 后端把事件累积到会话；停止时生成：
   - SOP 提示词（写进应用的 system_prompt）
   - agent-browser 回放命令清单（确定性重放用）
"""

import asyncio
import json
import os
import tempfile
import threading
import time
import uuid
from typing import Optional, Dict, List, Any

from cua_server.utils.logger import logger

# ---------------- 录制会话存储 ----------------

# 单会话录制（全局同时只允许一个录制会话，简单可靠）
_recording: Optional[Dict[str, Any]] = None


def _new_session(name_hint: str = "") -> dict:
    return {
        "id": f"rec_{uuid.uuid4().hex[:12]}",
        "status": "recording",  # recording | stopped
        "started_at": time.time(),
        "name_hint": name_hint,
        "events": [],       # 原始事件
        "pages": [],        # 访问过的 URL 序列
    }


def get_recording_session() -> Optional[dict]:
    return _recording


# ---------------- 注入脚本（轮询 eval 注入，URL 变化时重注入） ----------------

_RECORDER_JS = r"""
(function () {
  // 同一页面只安装一次监听。轮询线程会在 URL 变化时重注入，若不加此标记
  // 会叠加多个 listener，导致一次点击被记录成多条重复步骤。
  if (window.__cuTeachInstalled) return;
  window.__cuTeachInstalled = true;
  window.__cuTeachQueue = window.__cuTeachQueue || [];

  var q = window.__cuTeachQueue;
  var seq = 0;

  function send(ev) {
    // 画面交互透传（/interact）会自己直写一条完整事件；它设置的抑制窗口内
    // 不再重复上报，避免「钩子 1 条 + 直写 1 条」的双份步骤。
    if (window.__cuInteractUntil && Date.now() < window.__cuInteractUntil) return;
    try {
      ev.ts = Date.now();
      ev.url = location.href;
      ev.seq = ++seq;
      ev.source = 'hook';
      q.push(ev);
    } catch (e) {}
  }

  function fmtSel(el) {
    try {
      if (!el || el === document) return '';
      if (el.id) return '#' + el.id;
      if (el.name) {
        var byName = document.querySelectorAll('[name="' + el.name + '"]');
        if (byName.length === 1) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
      }
      var parts = [], cur = el, depth = 0;
      while (cur && cur !== document.body && depth < 4) {
        var s = cur.tagName ? cur.tagName.toLowerCase() : '';
        if (cur.id) { parts.unshift('#' + cur.id); break; }
        var parent = cur.parentElement;
        if (parent) {
          var sib = Array.prototype.filter.call(parent.children, function (c) { return c.tagName === cur.tagName; });
          if (sib.length > 1) s += ':nth-of-type(' + (Array.prototype.indexOf.call(sib, cur) + 1) + ')';
        }
        parts.unshift(s);
        cur = parent; depth++;
      }
      return parts.join(' > ') || '';
    } catch (e) { return ''; }
  }

  function describe(el) {
    try {
      return {
        tag: el && el.tagName ? el.tagName.toLowerCase() : '',
        // CLI 输出对非 ASCII 文本有编码转换问题，统一用 URI 编码传回，后端 unquote 还原
        text: encodeURIComponent(((el && (el.innerText || el.value)) || '').trim().slice(0, 40)),
        placeholder: encodeURIComponent((el && el.placeholder) || ''),
        aria: encodeURIComponent((el && el.getAttribute && el.getAttribute('aria-label')) || '')
      };
    } catch (e) { return {}; }
  }

  function interactive(el) {
    if (!el || !el.tagName) return null;
    var t = el.tagName.toLowerCase();
    if (t === 'a' || t === 'button' || t === 'select' || t === 'label') return el;
    if (el.getAttribute && (el.getAttribute('role') === 'button' || el.hasAttribute('onclick'))) return el;
    if (t === 'input' && ['submit', 'button', 'checkbox', 'radio'].indexOf(el.type) >= 0) return el;
    var c = el.closest ? el.closest('a,button,[role=button],[onclick]') : null;
    return c;
  }

  document.addEventListener('click', function (e) {
    var el = interactive(e.target);
    if (!el) return;
    send({
      type: 'click',
      selector: fmtSel(el),
      describe: describe(el),
      href: el.tagName === 'A' ? el.getAttribute('href') : undefined
    });
  }, true);

  document.addEventListener('change', function (e) {
    var el = e.target;
    if (!el || !el.tagName) return;
    var tag = el.tagName.toLowerCase();
    if ((tag === 'input' || tag === 'textarea') &&
        ['checkbox', 'radio', 'submit', 'button'].indexOf(el.type) < 0) {
      send({
        type: 'fill',
        selector: fmtSel(el),
        describe: describe(el),
        value: String(el.value == null ? '' : el.value).slice(0, 120)
      });
    } else if (tag === 'select') {
      send({
        type: 'select',
        selector: fmtSel(el),
        describe: describe(el),
        value: el.value != null ? String(el.value).slice(0, 120) : '',
        label: encodeURIComponent((el.selectedOptions && el.selectedOptions[0] && el.selectedOptions[0].text || '').slice(0, 60))
      });
    }
  }, true);

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      var tag = (e.target && e.target.tagName || '').toLowerCase();
      if (tag === 'input') {
        send({ type: 'press_enter', selector: fmtSel(e.target), describe: describe(e.target) });
      }
    }
  }, true);
})();
"""


# ---------------- CLI 交互 ----------------

def _run_cli(args: list, timeout: int = 15) -> str:
    import subprocess, shutil, os
    if not shutil.which("agent-browser"):
        return "error: agent-browser CLI not found"
    try:
        result = subprocess.run(
            ["agent-browser"] + args,
            capture_output=True, text=True,
            timeout=timeout, env=os.environ.copy(),
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if result.returncode != 0:
            return f"error: {(err or out or 'exit %d' % result.returncode)[:1500]}"
        return out or "(no output)"
    except subprocess.TimeoutExpired:
        return f"error: timeout after {timeout}s"
    except Exception as e:
        return f"error: {str(e)[:300]}"


def _eval_js(js: str, timeout: int = 15) -> str:
    """通过 base64 方式执行 JS，绕开 CLI 参数解析对引号/花括号的破坏。"""
    import base64
    b64 = base64.b64encode(js.encode("utf-8")).decode("ascii")
    return _run_cli(["eval", "-b", b64], timeout=timeout)


_DRAIN_JS = "JSON.stringify((window.__cuTeachQueue || []).splice(0))"


def _drain_events() -> list:
    """取走页面事件队列中的全部事件。"""
    out = _eval_js(_DRAIN_JS)
    if out.startswith("error:"):
        return []
    try:
        # 逐字段反转：队列里可能同时有直写事件（正常中文）和钩子事件（mojibake），
        # 整体 encode('latin-1') 会被正常中文打断，必须对每个字符串单独尝试。
        def fix(v):
            if isinstance(v, str):
                try:
                    return v.encode("latin-1").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    return v
            if isinstance(v, list):
                return [fix(i) for i in v]
            if isinstance(v, dict):
                return {k: fix(x) for k, x in v.items()}
            return v

        data = fix(json.loads(out))
        # agent-browser eval 的返回值是 JSON 字符串的再序列化（双层编码）：
        # CLI 输出 '"[{...}]"' → 第一层解析得到 '[{...}]' 字符串 → 第二层才是数组
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def get_start_url() -> str:
    out = _run_cli(["get", "url"])
    return "" if out.startswith("error:") else out.strip()


# ---------------- 事件接收（保留：beacon 端点兼容，当前主链路走轮询 drain） ----------------

def ingest_event(ev: dict) -> int:
    """接收页面上报的事件，写入当前录制会话。返回当前事件总数；无会话返回 -1。

    两类来源：页面钩子（source=hook）与画面交互透传直写（source=interact）。
    直写必须补齐 ts/seq，否则 stop 时按 ts 排序会把它们全排到最前面，
    多步示教的步骤顺序会错乱。另做一层短窗口去重兜底。
    """
    global _recording
    if not _recording or _recording["status"] != "recording":
        return -1
    ev = dict(ev)
    if not ev.get("ts"):
        ev["ts"] = int(time.time() * 1000)
    if not ev.get("source"):
        ev["source"] = "interact"

    # 页面文本经 encodeURIComponent 传回（绕开 agent-browser CLI 的编码转换），
    # 在入库前统一解码，保证 SOP / 回放命令里的中文可读。
    from urllib.parse import unquote
    d = ev.get("describe")
    if isinstance(d, dict):
        d = dict(d)
        for k in ("text", "placeholder", "aria"):
            v = d.get(k)
            if isinstance(v, str) and "%" in v:
                try:
                    d[k] = unquote(v)
                except Exception:
                    pass
        ev["describe"] = d
    if isinstance(ev.get("label"), str) and "%" in ev["label"]:
        try:
            ev["label"] = unquote(ev["label"])
        except Exception:
            pass

    def _key(e):
        return (e.get("type"), e.get("selector") or "",
                str(e.get("value") or ""), str(e.get("href") or ""))

    key = _key(ev)
    now = ev["ts"]
    for prev in reversed(_recording["events"][-8:]):
        if _key(prev) != key:
            continue
        delta = now - (prev.get("ts") or 0)
        if 0 <= delta < 500:
            return len(_recording["events"])

    url = ev.get("url") or ""
    if not url and _recording["pages"]:
        url = _recording["pages"][-1]   # 直写事件未带 URL 时沿用当前页
        ev["url"] = url
    if url:
        pages = _recording["pages"]
        if not pages or pages[-1] != url:
            pages.append(url)
    _recording["events"].append(ev)
    return len(_recording["events"])


# ---------------- 事件 → 动作清单 / SOP ----------------

def build_replay_commands(events: List[dict], start_url: str = "") -> List[dict]:
    """把捕获的原始事件转换为 agent-browser 回放命令清单。

    命令尾部以 #> hint 注释携带元素提示（供回放引擎自愈用），
    格式：click #sel #> Element Domains
    """
    cmds: List[dict] = []
    # 与 SOP 的「0. 打开起始页」保持一致：回放第一步必须回到示教起点，
    # 否则浏览器停留在任意页面时，第一条 click 就会找不到元素。
    if start_url and len(start_url) <= 2000 and not (events and events[0].get("type") == "navigate"):
        cmds.append({"cmd": f"open {start_url}", "why": "打开示教起始页"})
    for ev in events:
        t = ev.get("type")
        d = ev.get("describe") or {}
        hint = (d.get("text") or d.get("placeholder") or d.get("aria") or "").strip()[:40]
        if t == "navigate":
            cmds.append({"cmd": f"open {ev.get('url', '')}", "why": "导航"})
        elif t == "click":
            sel = ev.get("selector", "")
            if sel:
                suffix = f"  #> {hint}" if hint else ""
                cmds.append({"cmd": f"click {sel}{suffix}", "why": f"点击 {hint[:30]}"})
        elif t == "fill":
            sel = ev.get("selector", "")
            val = (ev.get("value") or "")
            if sel:
                suffix = f"  #> {d.get('placeholder') or hint}" if (d.get('placeholder') or hint) else ""
                cmds.append({"cmd": f"fill {sel} {json.dumps(val, ensure_ascii=False)}{suffix}", "why": f"填写 {(d.get('placeholder') or '')[:30]}"})
        elif t == "select":
            sel = ev.get("selector", "")
            val = (ev.get("value") or "")
            if sel and val:
                label = (ev.get("label") or val)[:30]
                cmds.append({"cmd": f"select {sel} {json.dumps(val, ensure_ascii=False)}  #> {label}", "why": f"选择 {label}"})
        elif t == "press_enter":
            cmds.append({"cmd": "press Enter", "why": "回车提交"})
        elif t == "scroll":
            dy = int(ev.get("dy") or 0)
            cmds.append({"cmd": f"mouse wheel {dy}", "why": "滚动页面"})
        elif t == "key":
            cmds.append({"cmd": f"press {ev.get('key') or 'Enter'}", "why": "按键"})
    return cmds


def build_sop(events: List[dict], start_url: str = "") -> str:
    """把动作清单归纳为自然语言 SOP（供 LLM 回放时理解整体意图）。"""
    steps: List[str] = []
    for i, ev in enumerate(events, 1):
        t = ev.get("type")
        d = ev.get("describe") or {}
        hint = d.get("text") or d.get("placeholder") or d.get("aria") or d.get("tag") or ""
        if t == "navigate":
            steps.append(f"{i}. 打开页面 {ev.get('url', '')}")
        elif t == "click":
            steps.append(f"{i}. 点击「{hint[:30]}」（选择器 {ev.get('selector', '')}）")
        elif t == "fill":
            steps.append(f"{i}. 在输入框「{hint[:30] or ev.get('selector','')}」填入：{ev.get('value', '')}")
        elif t == "select":
            steps.append(f"{i}. 在下拉框选择：{ev.get('label') or ev.get('value', '')}")
        elif t == "press_enter":
            steps.append(f"{i}. 按回车提交")
        elif t == "scroll":
            dy = int(ev.get("dy") or 0)
            steps.append(f"{i}. 页面{'向下' if dy >= 0 else '向上'}滚动 {abs(dy)} 像素")
        elif t == "key":
            steps.append(f"{i}. 按下 {ev.get('key') or 'Enter'} 键")
    if start_url and not (events and events[0].get("type") == "navigate"):
        steps.insert(0, f"0. 打开起始页 {start_url}")
    header = (
        "你是一个网页自动化应用，按照以下用户示教的步骤顺序操作网页完成固定任务。"
        "尽量按步骤执行；如果某一步元素找不到或页面结构变化，用 browser_snapshot 重新定位相似元素并继续；"
        "遇到验证码或登录页时暂停并告知用户。\n\n【示教的操作步骤】\n"
    )
    return header + "\n".join(steps)


# ---------------- 录制生命周期 ----------------

def start_recording(name_hint: str = "") -> dict:
    """开始录制：eval 注入事件捕获钩子 + 异步轮询回收事件。

    注：agent-browser 的 init-script（env / CLI 参数）在本版本 daemon 架构下不生效，
    故采用「轮询 eval 注入 + URL 变化时重注入」策略。钩子将事件写入页面内存队列，
    轮询每 0.5s drain 一次；导航瞬间旧页面销毁可能丢最近 1 个事件，
    由 URL 变化生成的 navigate 动作兜底（pages 列表）。
    """
    global _recording
    if _recording and _recording["status"] == "recording":
        return {"error": "已有录制会话进行中", "session": _recording}

    # 确认浏览器可用
    start_url = get_start_url()
    if not start_url:
        out = _run_cli(["open", "https://example.com"], timeout=45)
        if out.startswith("error:"):
            return {"error": f"浏览器不可用: {out[:200]}"}
        start_url = get_start_url()

    _recording = _new_session(name_hint)
    _recording["pages"].append(start_url)

    # 首次注入
    inject_out = _eval_js(_RECORDER_JS)
    if inject_out.startswith("error:"):
        _recording = None
        return {"error": f"注入录制钩子失败: {inject_out[:200]}"}

    # 启动轮询线程：录制的生命周期 = 线程生命周期，直接用 daemon 线程跑同步循环
    # （FastAPI sync 端点在线程池里执行，拿不到主事件循环，独立线程最稳）
    session_id = _recording["id"]
    t = threading.Thread(target=_poll_loop_sync, args=(session_id,), daemon=True)
    t.start()
    logger.info(f"[TeachRecorder] 轮询线程已启动 session={session_id}")

    return {
        "session": {k: v for k, v in _recording.items() if not k.startswith("_")},
        "start_url": start_url,
        "inject": inject_out[:60],
    }


def _poll_loop_sync(session_id: str):
    """同步轮询循环（跑在独立 daemon 线程里）：drain 事件 + URL 变化时重新注入钩子。"""
    global _recording
    last_url = ""
    inject_count = 0
    try:
        while _recording and _recording["id"] == session_id and _recording["status"] == "recording":
            try:
                url = get_start_url()
                url_changed = url and url != last_url
                if url_changed:
                    last_url = url
                    pages = _recording["pages"]
                    if not pages or pages[-1] != url:
                        pages.append(url)
                    # 新页面 → 重新注入钩子
                    inject_out = _eval_js(_RECORDER_JS)
                    inject_count += 1
                    logger.info(f"[TeachRecorder] 重注入 #{inject_count} @ {url[:60]} -> {inject_out[:40]}")

                evs = _drain_events()
                if evs:
                    _recording["events"].extend(evs)
                    logger.info(f"[TeachRecorder] 收到 {len(evs)} 个事件, 总计 {len(_recording['events'])}")
            except Exception as e:
                logger.warning(f"[TeachRecorder] poll error: {str(e)[:150]}")
            time.sleep(0.6)
    except Exception as e:
        logger.warning(f"[TeachRecorder] poll loop exit: {str(e)[:150]}")


def stop_recording() -> dict:
    """停止录制并生成动作清单 + SOP。"""
    global _recording
    if not _recording or _recording["status"] != "recording":
        return {"error": "没有进行中的录制会话"}

    _recording["status"] = "stopped"
    events = sorted(_recording["events"], key=lambda e: e.get("ts", 0) or 0)
    start_url = _recording["pages"][0] if _recording["pages"] else ""

    result = {
        "session": {k: v for k, v in _recording.items() if not k.startswith("_")},
        "replay_commands": build_replay_commands(events, start_url),
        "sop": build_sop(events, start_url),
        "event_count": len(events),
    }

    # 轮询循环检测到 status=stopped 后自动退出（drain 剩余事件已被上面的 sorted 收走）
    _recording = None
    return result


def cancel_recording() -> dict:
    global _recording
    if not _recording:
        return {"error": "没有录制会话"}
    _recording["status"] = "stopped"
    n = len(_recording.get("events", []))
    _recording = None
    return {"cancelled": True, "event_count": n}


def peek_recording() -> dict:
    """录制中实时查看已捕获的事件。"""
    if not _recording:
        return {"error": "没有录制会话"}
    events = sorted(_recording["events"], key=lambda e: e.get("ts", 0) or 0)
    return {
        "session": {
            "id": _recording["id"],
            "status": _recording["status"],
            "started_at": _recording["started_at"],
            "pages": _recording["pages"][-5:],
        },
        "events": [
            {
                "type": e.get("type"),
                "selector": (e.get("selector") or "")[:80],
                "hint": ((e.get("describe") or {}).get("text")
                         or (e.get("describe") or {}).get("placeholder")
                         or (e.get("describe") or {}).get("tag", ""))[:40],
                "value": (e.get("value") or "")[:40],
            }
            for e in events[-50:]
        ],
        "total": len(events),
    }
