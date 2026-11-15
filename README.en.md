<p align="center">
  <a href="README.md">简体中文</a> · <b>English</b>
</p>

<div align="center">

# 🖥️ Computer Use Agent

**Teach once, replay forever — a browser automation agent that learns flows by demonstration (CDP + Hermes dual execution layers)**

[![CI](https://github.com/wanshoulu/Computer-Use-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/wanshoulu/Computer-Use-Agent/actions/workflows/ci.yml)
[![Upstream](https://img.shields.io/badge/browser%2Fcomputer__use-NousResearch%2Fhermes--agent%20v0.16.0-blue)](https://github.com/NousResearch/hermes-agent)
[![License](https://img.shields.io/badge/license-MIT%20(upstream)%20%2B%20Apache--2.0%20(this%20repo)-green)](LICENSE)
[![Server](https://img.shields.io/badge/server-FastAPI%20%2B%20SSE%20task%20lifecycle-009688)](#-quick-start)
[![Web](https://img.shields.io/badge/web-console%20included-61dafb)](#-web-console)
[![Platform](https://img.shields.io/badge/browser-Chrome%20CDP%20%2B%20headless%20Chromium-2496ed)](https://chromedevtools.github.io/devtools-protocol/)

</div>

---

### What is this

**Computer Use Agent** turns browser automation into *teach once, replay forever*: you record your clicks and fills as a taught SOP, and an agent replays the flow automatically — re-locating elements via accessibility snapshots when the page shifts, pausing on captchas, and leaving a screenshot + audit trail for every step.

Two execution layers work together:

- **Deterministic CDP driver** — replay recorded flows with *zero* LLM calls; on a failed step, a confidence-ranked, multi-strategy self-healing routine kicks in, and a watchdog (page reload + LLM re-planning) fires after 3 consecutive failures.
- **Hermes Agent bridge** (LLM) — handles open-ended steps and re-planning, talking to the server over a stdin/stdout JSON protocol with a process pool for session isolation.

Everything runs through a FastAPI task state machine (`queued → planning → executing → waiting_user → done`) with SSE event streaming, real cancellation, per-task tab binding, and a full audit log.

![Teach & replay in action: 3 recorded steps → the agent executes them (click → type "北向资金" → submit → assert echoed output)](docs/demo-teach-replay.gif)

> Browser/desktop toolchain from [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT). The server (task state machine, CDP driver, process bridge, safety constraints) and web console are original work in this repo, licensed Apache-2.0. Full Chinese documentation: [README.md](README.md).

## ✨ Highlights

- **Taught SOP** is injected into the agent's system prompt — steps replay without code changes; failed steps are re-located automatically
- **Per-task tab isolation** — every task gets its own bound tab (TabBinding), closed on completion
- **Every step leaves evidence** — before/after screenshots + audit log, replayable afterwards
- **Dangerous-action interception** — URLs/params containing `delete / payment / confirm / submit` force user confirmation; hard cap of 30 tool calls per task
- **Scheduled sentinel** — "record once, run daily": recorded apps can be attached to cron-like schedules (SQLite storage)
- **7 preset finance scenarios** — HK quotes, fund NAV, IPO calendar, announcement watch, A-share gainers, northbound capital flows, research-report aggregation

## 🚀 Quick Start

**Prerequisites**: Python ≥ 3.11, Chrome, any OpenAI-compatible model endpoint (qwen-plus / deepseek / GPT / Claude).

```bash
# 1. Configure model credentials (env var or providers.json)
export CUA_MODEL_KEY="dashscope::qwen-plus"
export CUA_PROVIDER_DASHSCOPE_API_KEY="sk-..."

# 2. Launch Chrome with CDP + the server
chrome --remote-debugging-port=9222 --user-data-dir=/tmp/cua-chrome
cd server
export BROWSER_CDP_URL="http://127.0.0.1:9222"
pip install -r requirements.txt
uvicorn cua_server.app:app --host 0.0.0.0 --port 8000

# 3. Open http://localhost:8000/ — describe a task, or click "录制流程" to record a flow
```

Create tasks without the console:

```bash
curl -N -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"message":"Open example.com, click More information, report the new page title","model":"dashscope::qwen-plus"}'
```

## 🛡️ Safety

| Layer | Measure |
|---|---|
| Dangerous-action gate | `delete / payment / confirm / submit` patterns → wait for user confirmation |
| Financial-transaction guard | Hard prompt rule: never auto-submit forms involving financial transactions |
| Tool-call cap | 30 per task; exceeding → task FAILED (no runaway loops) |
| Retry cap | Max 3 retries per step before reporting failure |
| Isolation | One tab per task; Hermes sessions isolated per fingerprint workspace |
| Audit | Every tool call (params/duration/result) logged, queryable via `GET /api/tasks/{id}/audit` |
| Captchas | Detected → task enters `waiting_user`, pauses and notifies |

## 📚 Docs

| Doc | Content |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Design decisions & trade-offs, accessibility-tree vs pure-vision comparison, known limitations |
| [demo/README.md](demo/README.md) | Minimal CDP closed loop & trajectory format |
| [NOTICE.md](NOTICE.md) | Provenance: upstream Hermes code vs original work in this repo |

## 🙏 Credits & License

- **Browser/desktop toolchain**: [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) v0.16.0 (MIT), excerpted unmodified; desktop computer_use depends on [trycua/cua](https://github.com/trycua/cua) (MIT)
- **Server & web console** (`server/`): original to this repo, Apache-2.0
