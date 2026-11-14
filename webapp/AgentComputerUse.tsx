import { getProviderList } from '@/api/model';
import { agentAppsApi } from '@/api/agentApps';
import type { AgentAppInstance } from '@/types/agentApp';
import {
  CheckCircleOutlined,
  DesktopOutlined,
  ExpandOutlined,
  GlobalOutlined,
  LinkOutlined,
  LoadingOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  RightOutlined,
  RobotOutlined,
  SendOutlined,
  StopOutlined,
  ThunderboltOutlined,
  VideoCameraOutlined,
} from '@ant-design/icons';
import { Button, Input, Modal, Select, message } from 'antd';
import React, { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeRaw from 'rehype-raw';
import {
  cleanThinking,
  describeTool,
  explainFailure,
  formatDuration,
  summarizeProgress,
} from './toolDisplay';
import { mapStagePoint } from './stageCoords';
import './AgentComputerUse.css';

const { TextArea } = Input;

const AGENT_API_BASE = '/agent';

interface RecordedEvent {
  type: string;
  selector?: string;
  hint?: string;
  value?: string;
}

interface RecordingSession {
  id: string;
  status: string;
  pages: string[];
}

interface StopRecordingResult {
  event_count: number;
  sop: string;
  replay_commands: { cmd: string; why: string }[];
  detail?: string;
}

// ============ 会话持久化（localStorage，刷新恢复） ============
const SESSIONS_KEY = 'cua_sessions';
const ACTIVE_SESSION_KEY = 'cua_active_session';

function loadSessions(): ChatSession[] {
  try {
    const raw = localStorage.getItem(SESSIONS_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function saveSessions(sessions: ChatSession[], activeId: string | null) {
  try {
    // 只保留最近 20 个会话，每个会话最多 100 条消息
    const trimmed = sessions.slice(0, 20).map((s) => ({
      ...s,
      messages: s.messages.slice(-100),
    }));
    localStorage.setItem(SESSIONS_KEY, JSON.stringify(trimmed));
    // activeId 只在页面存续期间有效，刷新后回到空状态（见 activeSessionId 初始化）
    localStorage.removeItem(ACTIVE_SESSION_KEY);
  } catch {
    // 存储满等异常忽略
  }
}

// ============ 录制产物持久化（供回放） ============
const REPLAY_LIB_KEY = 'cua_replay_lib';

interface ReplayEntry {
  id: string;
  name: string;
  commands: string[];
  sop: string;
  savedAt: string;
}

function loadReplayLib(): ReplayEntry[] {
  try {
    const raw = localStorage.getItem(REPLAY_LIB_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveReplayLib(entries: ReplayEntry[]) {
  try {
    localStorage.setItem(REPLAY_LIB_KEY, JSON.stringify(entries.slice(-30)));
  } catch {
    // ignore
  }
}

interface ToolStep {
  id: string;
  name: string;
  status: 'running' | 'success' | 'error';
  durationMs?: number;
  message?: string;
  error?: string;
}

interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  thinkingContent?: string;
  toolSteps?: ToolStep[];
  status?: 'pending' | 'streaming' | 'done' | 'cancelled' | 'error';
  /** 任务期间收到的画面帧（降采样快照，最多 12 张），用于「回放时间线」 */
  replayFrames?: { data: string; at: number }[];
}


// ============ 回放时间线（Manus 式：任务结束后可拖拽回看每一步画面快照） ============
function ReplayTimeline({ frames }: { frames: { data: string; at: number }[] }) {
  const [idx, setIdx] = useState(0);
  const [open, setOpen] = useState(false);
  if (!frames || frames.length === 0) return null;
  const cur = frames[Math.min(idx, frames.length - 1)];
  const time = new Date(cur.at).toLocaleTimeString('zh-CN', { hour12: false });
  return (
    <div className="cu-replay">
      <button className="cu-replay-toggle" onClick={() => setOpen(!open)}>
        <PlayCircleOutlined />
        {open ? '收起回放' : `回放执行过程（${frames.length} 帧）`}
      </button>
      {open && (
        <div className="cu-replay-body">
          <img src={`data:image/jpeg;base64,${cur.data}`} alt={`第 ${idx + 1} 帧`} />
          <div className="cu-replay-ctrl">
            <button onClick={() => setIdx(Math.max(0, idx - 1))} disabled={idx === 0}>‹</button>
            <input
              type="range"
              min={0}
              max={frames.length - 1}
              value={idx}
              onChange={(e) => setIdx(Number(e.target.value))}
            />
            <button onClick={() => setIdx(Math.min(frames.length - 1, idx + 1))} disabled={idx >= frames.length - 1}>›</button>
            <span className="cu-replay-meta">{idx + 1}/{frames.length} · {time}</span>
          </div>
        </div>
      )}
    </div>
  );
}

interface ChatSession {
  id: string;
  title: string;
  messages: ChatMessage[];
  hermesSessionId?: string;
  createdAt: string;
}

/** 执行进度：给用户看中文一句话状态，原始工具日志收进折叠区 */
const ExecutionProgress: React.FC<{ steps: ToolStep[]; status?: ChatMessage['status'] }> = ({
  steps,
  status,
}) => {
  const finished = status === 'done' || status === 'error' || status === 'cancelled';
  const hasTools = steps.length > 0;
  if (!hasTools && !['pending', 'streaming'].includes(status || '')) return null;

  const summary = summarizeProgress(steps, finished);
  const doneCount = summary.visible.filter((v) => v.status === 'success').length;
  const recent = summary.visible.slice(-4);

  return (
    <div className={`cu-progress ${finished ? 'cu-progress-done' : 'cu-progress-running'}`}>
      <div className="cu-progress-head">
        <span className="cu-progress-icon">
          {finished ? <CheckCircleOutlined /> : <LoadingOutlined />}
        </span>
        <span className="cu-progress-title">
          {hasTools ? summary.headline : '正在启动浏览器，准备执行任务…'}
        </span>
        {summary.totalMs > 0 && (
          <span className="cu-progress-time">已用 {formatDuration(summary.totalMs)}</span>
        )}
      </div>

      {hasTools && (
        <div className="cu-progress-body">
          <div className="cu-progress-meta">
            已完成 {doneCount} 步
            {summary.hiddenCount > 0 && ` · 另有 ${summary.hiddenCount} 次页面检查`}
            {summary.failedCount > 0 && ` · ${summary.failedCount} 步自动重试过`}
          </div>

          {recent.length > 0 && (
            <ul className="cu-progress-steps">
              {recent.map((v) => (
                <li key={v.key} className={`cu-progress-step cu-step-${v.status}`}>
                  <span className="cu-step-dot" />
                  <span className="cu-step-label">{v.label}</span>
                  {v.status === 'running' && <span className="cu-step-tag">进行中</span>}
                  {v.status === 'error' && (
                    <span className="cu-step-tag cu-step-tag-warn" title={explainFailure({ name: v.label, error: v.error })}>
                      已换方式重试
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}

          <details className="cu-progress-log">
            <summary>查看详细日志（{steps.length} 条原始记录）</summary>
            <div className="cu-progress-log-body">
              {steps.map((s) => (
                <div key={s.id} className="cu-log-row">
                  <span className={`cu-log-status cu-log-${s.status}`}>
                    {s.status === 'running' ? '进行' : s.status === 'error' ? '失败' : '完成'}
                  </span>
                  <span className="cu-log-name">{s.name}</span>
                  <span className="cu-log-desc">{describeTool(s.name, s.message).label}</span>
                  {s.durationMs != null && (
                    <span className="cu-log-dur">{formatDuration(s.durationMs)}</span>
                  )}
                </div>
              ))}
            </div>
          </details>
        </div>
      )}
    </div>
  );
};

/** 模型思考：过滤掉颜文字 / reasoning... 之类的噪声，默认折叠 */
const ThinkingBlock: React.FC<{ raw?: string }> = ({ raw }) => {
  const text = cleanThinking(raw);
  if (!text) return null;
  return (
    <details className="cu-thinking">
      <summary>
        <ThunderboltOutlined />
        查看模型思考（可选）
      </summary>
      <div className="cu-thinking-body">{text}</div>
    </details>
  );
};

const BASE_SOP =
  '你是 Computer Use Agent，通过 browser 工具控制浏览器操作网页。Chrome 已通过 CDP 连接，无需确认连接状态，直接操作即可。' +
  '【效率要求】每次工具调用都要启动一次浏览器进程，开销约 1 秒，因此务必减少调用次数：' +
  '页面发生导航、提交或结构变化后观察一次页面（browser_snapshot），拿到元素引用后连续完成点击、填写、读取等多个动作，' +
  '不要在每个动作前后重复 snapshot；能用一次 browser_get_text / browser_eval 取到的数据不要分多次读取；' +
  '同一操作失败不要原样重试超过 1 次，换用其他选择器或工具。';

const DEFAULT_SYSTEM_PROMPT =
  BASE_SOP +
  '你可以导航到指定URL、点击元素、填写表单、提取页面内容。先观察当前页面再决定操作，遇到验证码或需要人工干预时暂停并告知用户。';

interface ScenarioCard {
  key: string;
  title: string;
  icon: string;
  desc: string;
  prompt: string;
  difficulty: '简单' | '中等' | '高难';
  sop: string;
}

const SCENARIOS: ScenarioCard[] = [
  {
    key: 'hk-stock',
    title: '港股行情速览',
    icon: '📈',
    desc: '新浪财经查腾讯控股实时行情',
    prompt: '查询腾讯控股(00700)港股实时行情，提取最新价、涨跌额、涨跌幅、成交量、成交额，汇总成简报',
    difficulty: '简单',
    sop:
      BASE_SOP +
      "\n\n【操作步骤·推荐数据源（优先用这个，比爬页面稳定）】\n1. browser_navigate 到 https://stock.finance.sina.com.cn/hkstock/quotes/00700.html（让浏览器带上行情域）\n2. 用 browser_eval 执行 fetch 获取腾讯控股实时行情：\n   fetch('https://qt.gtimg.cn/q=hk00700').then(r=>r.text())\n3. 返回文本用 ~ 分隔：第4段=最新价(419.800)、第5段=昨收(430.400)、第6段=今开、第37段附近=成交量、时间戳在第31段附近；涨跌额=最新价-昨收、涨跌幅=(涨跌额/昨收*100)%\n4. 解析：最新价、涨跌额、涨跌幅、成交量、成交额\n5. 汇总成简报输出\n【降级策略】若接口获取失败，直接读页面行情区域文本；仍失败则不要停下询问用户，说明数据源不可用即结束",
  },
  {
    key: 'fund-nav',
    title: '基金净值查询',
    icon: '💰',
    desc: '天天基金网查基金代码 110011 净值',
    prompt: '查询基金代码 110011 的最新单位净值、累计净值、净值日期和近一周涨跌幅，汇总成简报',
    difficulty: '简单',
    sop:
      BASE_SOP +
      "\n\n【操作步骤】\n1. browser_navigate 到 https://fundf10.eastmoney.com/jjjz_110011.html（基金历史净值页，带东财域）\n2. browser_eval 执行 fetch 获取净值接口：\n   fetch('https://api.fund.eastmoney.com/f10/lsjz?fundCode=110011&pageIndex=1&pageSize=8').then(r=>r.json())\n3. 解析 JSON：Data.LSJZList 数组，每条含 FSRQ=净值日期、DWJZ=单位净值、LJJZ=累计净值、JZZZL=日涨跌幅\n4. 最新一条即最新净值；用前几条算近一周涨跌幅（(最新-一周前)/一周前*100）\n5. 汇总成简报输出\n【降级策略】若接口失败，直接读页面上最新净值表格行；仍失败则说明数据源不可用即结束，不要停下询问用户",
  },
  {
    key: 'ipo',
    title: '新股申购信息',
    icon: '🚀',
    desc: '东方财富网今日可申购新股列表',
    prompt: '查询今日可申购新股列表，提取股票名称、申购代码、发行价格、申购上限，汇总成表格',
    difficulty: '简单',
    sop:
      BASE_SOP +
      "\n\n【操作步骤·推荐数据源（优先用这个，比爬页面稳定）】\n1. browser_navigate 到 https://data.eastmoney.com/xg/dxsyl.html（让浏览器带上东财域）\n2. 用 browser_eval 执行 fetch 调用东财数据接口：\n   fetch('https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_APP_IPOAPPLY&columns=SECURITY_NAME%2CAPPLY_CODE%2CISSUE_PRICE%2CONLINE_APPLY_UPPER%2CAPPLY_DATE&pageSize=30&sortColumns=APPLY_DATE&sortTypes=-1&pageNumber=1').then(r=>r.json())\n3. 解析 JSON：SECURITY_NAME=股票名称、APPLY_CODE=申购代码、ISSUE_PRICE=发行价、ONLINE_APPLY_UPPER=申购上限（股）、APPLY_DATE=申购日期\n4. 过滤 APPLY_DATE 为今天或最近的记录，汇总成表格输出\n【降级策略】若接口无数据，再访问页面表格提取；若页面也无数据，用 browser_search 搜索「今日新股申购列表」；不要停下询问用户",
  },
  {
    key: 'announcement',
    title: '上市公司公告监控',
    icon: '📋',
    desc: '巨潮资讯网搜索"银行"行业公告',
    prompt: '在巨潮资讯网搜索"银行"相关公告，提取前5条最新公告的标题、发布日期和关键内容摘要，汇总成报告',
    difficulty: '中等',
    sop:
      BASE_SOP +
      "\n\n【操作步骤】\n1. browser_navigate 到 https://www.cninfo.com.cn/new/fulltextSearch?notautosubmit=&keyWord=银行（巨潮全文搜索页，直接带关键词）\n2. browser_snapshot 查看搜索结果列表\n3. 对前5条：browser_get_text 提取标题、发布日期、摘要（前100字）\n4. 汇总成结构化报告\n【数据校验】报告必须包含至少 3 条真实公告标题；若结果为空或页面加载失败，换关键词「银行」重试一次，仍失败则明确说明巨潮数据源不可用即结束，不要编造公告标题，不要停下询问用户",
  },
  {
    key: 'stock-rank',
    title: 'A股涨幅榜抓取',
    icon: '🔥',
    desc: '东方财富涨幅榜前10只股票',
    prompt: '查询A股涨幅榜前10名，提取股票名称、代码、最新价、涨幅、成交额，汇总成表格',
    difficulty: '中等',
    sop:
      BASE_SOP +
      "\n\n【操作步骤·推荐数据源（优先用这个，比爬页面稳定）】\n1. browser_navigate 到 https://quote.eastmoney.com/center/gridlist.html（让浏览器带上东财域）\n2. 用 browser_eval 执行 fetch 获取涨幅榜：\n   fetch('https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=10&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:0+t:6%2Cm:0+t:80%2Cm:1+t:2%2Cm:1+t:23&fields=f2%2Cf3%2Cf5%2Cf6%2Cf12%2Cf14').then(r=>r.json())\n3. 解析 JSON：data.diff 数组，每条 f14=名称、f12=代码、f2=最新价、f3=涨幅%、f5=成交量、f6=成交额\n4. 汇总成表格输出前 10 名\n【降级策略】若接口失败，读页面表格前 10 行；仍失败则说明数据源不可用即结束，不要停下询问用户",
  },
  {
    key: 'northbound',
    title: '北向资金动向',
    icon: '🌐',
    desc: '近5个交易日北向资金流入流出',
    prompt: '查询北向资金近5个交易日的每日净买入额、累计净买入额，汇总成趋势简报',
    difficulty: '中等',
    sop:
      BASE_SOP +
      "\n\n【操作步骤·推荐数据源（优先用这个，比爬页面稳定）】\n1. browser_navigate 到 https://data.eastmoney.com/hsgt/index.html（让浏览器带上东财域）\n2. 用 browser_eval 执行 fetch 获取北向资金：\n   fetch('https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_MUTUAL_DEAL_HISTORY&columns=TRADE_DATE%2CMUTUAL_TYPE%2CNET_DEAL_AMT%2CACCUM_DEAL_AMT&pageSize=15&pageNumber=1&sortColumns=TRADE_DATE&sortTypes=-1').then(r=>r.json())\n3. 解析 JSON：data 数组，MUTUAL_TYPE 001=沪股通、003=深股通（北向合计=两者相加）、TRADE_DATE=日期、NET_DEAL_AMT=净买入额\n4. 取近 5 个交易日，按日期分组计算每日北向净买入（沪+深），汇总趋势简报\n【降级策略】若接口失败，读页面北向资金表格；仍失败则说明数据源不可用即结束，不要停下询问用户",
  },
  {
    key: 'research-aggregate',
    title: '行业研报深度聚合',
    icon: '📊',
    desc: '半导体行业3篇研报交叉验证',
    prompt: '搜索"半导体"行业最新研报，提取3篇研报的核心观点、评级、目标价，交叉验证后输出行业共识',
    difficulty: '高难',
    sop:
      BASE_SOP +
      "\n\n【操作步骤·推荐数据源（优先用这个，比爬页面稳定）】\n1. browser_navigate 到 https://data.eastmoney.com/report/（让浏览器带上东财域）\n2. 用 browser_eval 执行 fetch 获取研报列表：\n   fetch('https://reportapi.eastmoney.com/report/list?industryCode=*&pageSize=10&industry=%E5%8D%8A%E5%AF%BC%E4%BD%93&rating=&ratingChange=&beginTime=2026-11-01&endTime=2026-11-08&pageNo=1&qType=1').then(r=>r.json())\n3. 解析 JSON：data 数组，每条 title=标题、orgSName=券商、rating=评级、publishDate=日期\n4. 选前 3 篇，browser_navigate 到 infoCode 对应详情页 https://data.eastmoney.com/report/info/{infoCode}.html 逐篇提取核心观点与目标价\n5. 交叉验证：评级一致性、目标价区间、观点异同，输出行业研报共识报告\n【降级策略】若接口失败，在页面上搜索并逐篇读取；仍失败则说明数据源不可用即结束，不要停下询问用户",
  },
  {
    key: 'company-profile',
    title: '上市公司全景画像',
    icon: '🏢',
    desc: '宁德时代跨三站点多维度聚合',
    prompt: '为"宁德时代(300750)"生成全景画像：最新公告、核心财务指标、主力资金流向，多维度汇总',
    difficulty: '高难',
    sop:
      BASE_SOP +
      "\n\n【操作步骤·推荐数据源（优先用这个，比爬多站点稳定）】\n1. browser_navigate 到 https://data.eastmoney.com/zjlx/300750.html（让浏览器带上东财域）\n2. 用 browser_eval 分两步 fetch：\n   a) 核心指标：fetch('https://push2.eastmoney.com/api/qt/stock/get?secid=0.300750&fields=f57%2Cf58%2Cf105%2Cf162%2Cf167%2Cf183%2Cf184').then(r=>r.json())\n      解析：f58=名称、f105=净利润(元)、f162=PE(除以100)、f167=市净率(除以100)、f183=营收(元)、f184=营收同比%\n   b) 最新公告：fetch('https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size=5&page_index=1&ann_type=A&client_source=web&stock_list=300750').then(r=>r.json())\n      解析：data.list 数组，title=公告标题、notice_date=日期\n3. 三维度整合（财务指标/公告/资金），输出全景画像\n【降级策略】若接口失败，再访问巨潮搜公告与 F10 页面读指标；仍失败则说明数据源不可用即结束，不要停下询问用户",
  },
  {
    key: 'ipo-tracking',
    title: 'IPO审核进度追踪',
    icon: '🔍',
    desc: '证监会+沪深交易所多站点遍历',
    prompt: '追踪"半导体"行业公司的IPO审核进度，从证监会和交易所获取最新审核状态，汇总成进度表',
    difficulty: '高难',
    sop:
      BASE_SOP +
      "\n\n【操作步骤·推荐数据源（优先用这个，比爬多站点稳定）】\n1. browser_navigate 到 https://data.eastmoney.com/xg/dxsyl.html（让浏览器带上东财域）\n2. 用 browser_eval 执行 fetch 获取 IPO 审核进度：\n   fetch('https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_IPO_PROGRRESS&columns=ALL&pageSize=20&pageNumber=1&sortColumns=APPLY_DATE&sortTypes=-1').then(r=>r.json())\n   若该报表不存在（code 9501），改用：fetch('https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_APP_IPOAPPLY&columns=SECURITY_NAME%2CAPPLY_CODE%2CINDUSTRY_NAME%2CAPPLY_DATE%2CLISTING_DATE&pageSize=30&sortColumns=APPLY_DATE&sortTypes=-1&pageNumber=1').then(r=>r.json())\n3. 解析 JSON：过滤行业含「半导体」的记录，输出公司名/代码/受理日期/当前阶段进度表\n【降级策略】若两个接口都失败，browser_navigate 到证监会与交易所页面人工路径读取；仍失败则说明数据源不可用即结束，不要停下询问用户",
  },
];

const DIFFICULTY_STEPS: Record<string, string> = {
  简单: '1-3 步 · 单页提取',
  中等: '3-7 步 · 搜索翻页',
  高难: '8+ 步 · 多站聚合',
};

// 左侧应用侧边栏的统一条目：预置场景 + 录制的新应用
interface SidebarApp {
  key: string;
  title: string;
  icon: string;
  desc: string;
  prompt: string;
  sop?: string;
  preset: boolean; // true=预置场景, false=用户录制的新应用
}

const formatTime = (iso: string) => {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
};

const AgentComputerUsePage: React.FC = () => {
  const [models, setModels] = useState<{ value: string; label: string }[]>([]);
  const [selectedModelKey, setSelectedModelKey] = useState<string>();
  const [selectedApp, setSelectedApp] = useState<AgentAppInstance | null>(null);
  // 左侧应用侧边栏：预置场景 + 用户录制的新应用（来自服务器）
  const [customApps, setCustomApps] = useState<SidebarApp[]>([]);
  const [sessions, setSessions] = useState<ChatSession[]>(() => loadSessions());
  // 信息架构：刷新/重开页面一律回到空状态（欢迎页），历史会话保留在左侧
  // 「历史交互记录」列表里按需点开——不自动恢复上次打开的会话
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  const currentTaskIdRef = useRef<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<any>(null);

  const [browserFrame, setBrowserFrame] = useState<string | null>(null);
  const [browserStatus, setBrowserStatus] = useState('连接中…');
  const [lastFrameAt, setLastFrameAt] = useState<Date | null>(null);  // 最近收到帧的时间，用于面板上显示直播状态
  // 虚拟光标：Codex 式操控可视化——tool_start 携带目标选择器/元素引用时，
  // 画面上显示一个"正在移动到目标"的光标 + 目标高亮，任务结束或 3 秒无新动作后淡出
  const [agentCursor, setAgentCursor] = useState<{ label: string; at: number } | null>(null);
  // 回放缓冲：任务进行中持续采样帧，任务结束时快照进消息
  const replayBufRef = useRef<{ data: string; at: number }[]>([]);
  const [browserTarget, setBrowserTarget] = useState<{ title?: string; url?: string }>({});
  // 浏览器画面视图：panel=默认展开（用户必须能直接看到实时画面）、
  // hidden=收起为竖条、max=全屏放大。窄窗口自动用小面板，避免挤压对话区。
  const [browserView, setBrowserView] = useState<'hidden' | 'panel' | 'max'>('panel');
  const [browserPanelWidth, setBrowserPanelWidth] = useState(() =>
    typeof window !== 'undefined' && window.innerWidth < 1360 ? 380 : 560,
  );

  // 示教录制
  const [recording, setRecording] = useState<RecordingSession | null>(null);
  const [recordedEvents, setRecordedEvents] = useState<RecordedEvent[]>([]);
  const [recordPeekTimer, setRecordPeekTimer] = useState<ReturnType<typeof setInterval> | null>(null);
  const [saveAppModalOpen, setSaveAppModalOpen] = useState(false);
  const [saveAppForm, setSaveAppForm] = useState({ name: '', sop: '', modelKey: '' });
  const [savingApp, setSavingApp] = useState(false);
  const [lastReplayCommands, setLastReplayCommands] = useState<string[]>([]);
  const [replayRunning, setReplayRunning] = useState(false);
  const [replayProgress, setReplayProgress] = useState<{ index: number; total: number; status: string; heal?: string }[]>([]);
  const [replaySummary, setReplaySummary] = useState<string>('');

  useEffect(() => {
    loadModels();
    loadApps();
  }, []);

  useEffect(() => {
    let es: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let closed = false;

    const connect = () => {
      if (closed) return;
      setBrowserStatus('连接中…');
      es = new EventSource(`${AGENT_API_BASE}/browser/view`);
      es.onmessage = (e) => {
        try {
          const ev = JSON.parse(e.data);
          if (ev.type === 'frame') {
            setBrowserFrame(ev.data);
            setBrowserStatus('');
            setLastFrameAt(new Date());
            // 回放时间线采样：任务进行中每 4 帧存一张快照（上限 12 张，省 localStorage）
            replayBufRef.current.push({ data: ev.data, at: Date.now() });
            if (replayBufRef.current.length > 48) replayBufRef.current.shift();
          } else if (ev.type === 'status') {
            setBrowserStatus(ev.message || '');
          } else if (ev.type === 'target') {
            setBrowserTarget({ title: ev.title, url: ev.url });
          }
        } catch {
          // ignore
        }
      };
      es.onerror = () => {
        es?.close();
        setBrowserFrame(null);
        setBrowserStatus('连接中断，3 秒后重连…');
        if (!closed) {
          reconnectTimer = setTimeout(connect, 3000);
        }
      };
    };

    connect();

    return () => {
      closed = true;
      es?.close();
      if (reconnectTimer) clearTimeout(reconnectTimer);
    };
  }, []);

  useEffect(() => {
    // 只滚动消息区容器，不用 scrollIntoView——后者会滚动所有可滚祖先
    // （包括整页 window），把右侧浏览器画面也跟着顶上去
    const el = messagesEndRef.current;
    if (!el) return;
    const list = el.closest('.cu-messages') as HTMLElement | null;
    if (list) {
      list.scrollTo({ top: list.scrollHeight, behavior: 'smooth' });
    }
  }, [sessions, activeSessionId]);

  // 会话持久化：sessions 或 activeSessionId 变化时保存
  useEffect(() => {
    saveSessions(sessions, activeSessionId);
  }, [sessions, activeSessionId]);

  const loadModels = async () => {
    try {
      const providers = await getProviderList();
      const modelOptions: { value: string; label: string }[] = [];
      for (const p of providers) {
        if (!p.models) continue;
        for (const m of p.models) {
          modelOptions.push({
            value: `${p.id}::${m.modelId}`,
            label: `${p.name} / ${m.name}`,
          });
        }
      }
      setModels(modelOptions);
      if (modelOptions.length > 0 && !selectedModelKey) {
        setSelectedModelKey(modelOptions[0].value);
      }
    } catch {
      message.error('加载模型列表失败');
    }
  };

  const loadApps = async () => {
    try {
      const serverApps = await agentAppsApi.list();
      const computerUseApps = serverApps
        .filter((a) => a.type === 'COMPUTER_USE' && a.status === 'ACTIVE')
        .map((a) => ({
          id: a.id,
          name: a.name,
          type: 'computer_use' as const,
          modelKey: a.modelKey || '',
          knowledgeBaseIds: [],
          systemPrompt: a.systemPrompt || DEFAULT_SYSTEM_PROMPT,
          tools: [],
          createdAt: a.createdAt || new Date().toISOString(),
          status: a.status as any,
        }));
      if (computerUseApps.length > 0) {
        setSelectedApp(computerUseApps[0]);
      }
      // 录制的新应用进左侧侧边栏（排除预置的 app-computer-use 本体）
      const customs: SidebarApp[] = serverApps
        .filter((a) => a.type === 'COMPUTER_USE' && a.status === 'ACTIVE' && a.id !== 'app-computer-use')
        .map((a) => ({
          key: a.id,
          title: a.name,
          icon: '🎬',
          desc: `录制应用 · ${new Date(a.createdAt || Date.now()).toLocaleDateString('zh-CN')}`,
          prompt: `按照「${a.name}」应用的示教步骤执行任务`,
          sop: a.systemPrompt || DEFAULT_SYSTEM_PROMPT,
          preset: false,
        }));
      setCustomApps(customs);
    } catch {
      // 非致命
    }
  };

  const handleNewSession = () => {
    const session: ChatSession = {
      id: `s_${Date.now()}`,
      title: '新对话',
      messages: [],
      createdAt: new Date().toISOString(),
    };
    setSessions((prev) => [session, ...prev]);
    setActiveSessionId(session.id);
    inputRef.current?.focus();
  };

  const activeSession = sessions.find((s) => s.id === activeSessionId);

  const handleSend = async (overrideText?: string, titleOverride?: string, sopOverride?: string) => {
    const textToSend = overrideText ?? input;
    if (!textToSend.trim()) return;
    if (!selectedModelKey) {
      message.warning('请先选择模型');
      return;
    }

    // 任务一开始就把实时画面展开，用户不该自己去点开一条窄竖条
    setBrowserView((v) => (v === 'max' ? v : 'panel'));

    let sessionId = activeSessionId;
    if (!sessionId) {
      const session: ChatSession = {
        id: `s_${Date.now()}`,
        title: titleOverride || textToSend.slice(0, 30),
        messages: [],
        createdAt: new Date().toISOString(),
      };
      setSessions((prev) => [session, ...prev]);
      sessionId = session.id;
      setActiveSessionId(session.id);
    }

    const userMsg: ChatMessage = {
      id: `m_${Date.now()}`,
      role: 'user',
      content: textToSend,
      status: 'done',
    };
    const assistantMsgId = `m_${Date.now()}_a`;
    const assistantMsg: ChatMessage = {
      id: assistantMsgId,
      role: 'assistant',
      content: '',
      status: 'pending',
    };

    const currentInput = textToSend;
    setInput('');

    setSessions((prev) =>
      prev.map((s) =>
        s.id === sessionId
          ? {
              ...s,
              title: s.title || currentInput.slice(0, 30),
              messages: [...s.messages, userMsg, assistantMsg],
            }
          : s,
      ),
    );

    setLoading(true);
    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    try {
      const systemPrompt = sopOverride || selectedApp?.systemPrompt || DEFAULT_SYSTEM_PROMPT;

      const response = await fetch(`${AGENT_API_BASE}/browser/tasks`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: currentInput,
          model: selectedModelKey,
          system_prompt: systemPrompt,
          session_id: activeSession?.hermesSessionId || undefined,
        }),
        signal: abortController.signal,
      });

      if (!response.ok) {
        const errText = await response.text();
        throw new Error(errText || `HTTP ${response.status}`);
      }

      const reader = response.body?.getReader();
      if (!reader) throw new Error('无法读取响应流');

      const decoder = new TextDecoder();
      let buffer = '';
      const toolStepsMap = new Map<string, ToolStep>();
      let thinkingParts: string[] = [];

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const event = JSON.parse(line);

            // 从任意事件中捕获 task_id（第一个事件通常是 snapshot）
            if (event.task_id && !currentTaskIdRef.current) {
              currentTaskIdRef.current = event.task_id;
            }

            if (event.type === 'tool_start') {
              const stepId = event.step_id || event.tool_call_id || `t_${Date.now()}`;
              toolStepsMap.set(stepId, {
                id: stepId,
                name: event.name || '工具',
                status: 'running',
                message: event.message || '',
              });
              // 虚拟光标事件：把当前动作映射为人类可读的操控提示
              {
                const CURSOR_LABELS: Record<string, string> = {
                  browser_click: '点击',
                  browser_fill: '输入',
                  browser_navigate: '打开',
                  browser_scroll: '滚动',
                  browser_select: '选择',
                  browser_press: '按键',
                };
                const act = CURSOR_LABELS[event.name];
                if (act) {
                  const target = event.cursor?.selector || event.cursor?.ref || event.cursor?.coordinate || '';
                  setAgentCursor({
                    label: target ? `${act} ${String(target).slice(0, 30)}` : act,
                    at: Date.now(),
                  });
                }
              }
              setSessions((prev) =>
                prev.map((s) =>
                  s.id === sessionId
                    ? {
                        ...s,
                        messages: s.messages.map((m) =>
                          m.id === assistantMsgId
                            ? { ...m, toolSteps: Array.from(toolStepsMap.values()) }
                            : m,
                        ),
                      }
                    : s,
                ),
              );
            } else if (event.type === 'tool_end') {
              const stepId = event.step_id || event.tool_call_id || '';
              const existing = toolStepsMap.get(stepId);
              if (existing) {
                existing.status = event.success === false ? 'error' : 'success';
                existing.durationMs = event.duration_ms;
                existing.error = event.error || event.message || '';
              }
              setSessions((prev) =>
                prev.map((s) =>
                  s.id === sessionId
                    ? {
                        ...s,
                        messages: s.messages.map((m) =>
                          m.id === assistantMsgId
                            ? { ...m, toolSteps: Array.from(toolStepsMap.values()) }
                            : m,
                        ),
                      }
                    : s,
                ),
              );
            } else if (event.type === 'content') {
              const text = event.content || '';
              const isComplete = event.status === 'response_complete';
              if (text) {
                setSessions((prev) =>
                  prev.map((s) =>
                    s.id === sessionId
                      ? {
                          ...s,
                          messages: s.messages.map((m) =>
                            m.id === assistantMsgId
                              ? {
                                  ...m,
                                  content: isComplete ? text : m.content + text,
                                  status: 'streaming',
                                }
                              : m,
                          ),
                        }
                      : s,
                  ),
                );
              }
            } else if (event.type === 'thinking') {
              thinkingParts.push(event.content || '');
              setSessions((prev) =>
                prev.map((s) =>
                  s.id === sessionId
                    ? {
                        ...s,
                        messages: s.messages.map((m) =>
                          m.id === assistantMsgId
                            ? { ...m, thinkingContent: thinkingParts.join('\n') }
                            : m,
                        ),
                      }
                    : s,
                ),
              );
            } else if (event.type === 'done') {
              const sessionFromEvent = event.session_id;
              // 回放时间线快照：把任务期间采样的帧均匀抽 12 张挂到消息上
              const buf = replayBufRef.current;
              const sampled: { data: string; at: number }[] = [];
              if (buf.length > 0) {
                const step = Math.max(1, Math.floor(buf.length / 12));
                for (let i = 0; i < buf.length && sampled.length < 12; i += step) {
                  sampled.push(buf[i]);
                }
              }
              replayBufRef.current = [];
              setSessions((prev) =>
                prev.map((s) =>
                  s.id === sessionId
                    ? {
                        ...s,
                        hermesSessionId: sessionFromEvent || s.hermesSessionId,
                        messages: s.messages.map((m) =>
                          m.id === assistantMsgId
                            ? {
                                ...m,
                                status: m.status === 'error' ? 'error' : 'done',
                                replayFrames: sampled.length > 0 ? sampled : m.replayFrames,
                              }
                            : m,
                        ),
                      }
                    : s,
                ),
              );
            } else if (event.type === 'error') {
              const errMsg = event.message || event.error || '未知错误';
              setSessions((prev) =>
                prev.map((s) =>
                  s.id === sessionId
                    ? {
                        ...s,
                        messages: s.messages.map((m) =>
                          m.id === assistantMsgId
                            ? { ...m, content: errMsg, status: 'error' }
                            : m,
                        ),
                      }
                    : s,
                ),
              );
            }
          } catch {
            // skip
          }
        }
      }
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        setSessions((prev) =>
          prev.map((s) =>
            s.id === sessionId
              ? {
                  ...s,
                  messages: s.messages.map((m) =>
                    m.id === assistantMsgId
                      ? { ...m, content: err.message || '请求失败', status: 'error' }
                      : m,
                  ),
                }
              : s,
          ),
        );
      } else {
        setSessions((prev) =>
          prev.map((s) =>
            s.id === sessionId
              ? {
                  ...s,
                  messages: s.messages.map((m) =>
                    m.id === assistantMsgId
                      ? { ...m, status: 'cancelled', content: m.content || '已取消' }
                      : m,
                  ),
                }
              : s,
          ),
        );
      }
    } finally {
      setLoading(false);
      currentTaskIdRef.current = null;
    }
  };

  const handleCancel = () => {
    if (currentTaskIdRef.current) {
      fetch(`${AGENT_API_BASE}/browser/tasks/${currentTaskIdRef.current}/cancel`, {
        method: 'POST',
      }).catch(() => {});
    }
    abortControllerRef.current?.abort();
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  // ==================== 示教录制 ====================

  const stopPeekTimer = () => {
    setRecordPeekTimer((prev) => {
      if (prev) clearInterval(prev);
      return null;
    });
  };

  // ============ 录制模式：画面交互透传（点击/滚轮/键盘 → 浏览器真实操作） ============
  const interactRef = useRef(false);
  const [recInput, setRecInput] = useState('');

  const postInteract = async (payload: Record<string, unknown>) => {
    try {
      const res = await fetch(`${AGENT_API_BASE}/browser/interact`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (data?.ok === false && data.error) message.warning(data.error);
      if (data?.hint) message.info(data.hint);
      return data;
    } catch {
      return null;
    }
  };

  const handleStageClick = async (e: React.MouseEvent<HTMLDivElement>) => {
    if (!recording || interactRef.current) return;
    const stage = e.currentTarget;
    stage.focus();
    const img = stage.querySelector('img');
    if (!img || !img.naturalWidth || !img.naturalHeight) return;
    // 画面是浏览器视口截图，CSS object-fit: contain 会在两侧留白。
    // 用图片自然尺寸 + 实际渲染框反算 letterbox，绝不能写死 1280x720：
    // 真实视口高度会随窗口变化（实测 1280x577），写死会导致每次点击都点偏。
    const rect = stage.getBoundingClientRect();
    const ir = img.getBoundingClientRect();
    const point = mapStagePoint({
      stage: { left: rect.left, top: rect.top, width: rect.width, height: rect.height },
      image: { left: ir.left, top: ir.top, width: ir.width, height: ir.height },
      naturalWidth: img.naturalWidth,
      naturalHeight: img.naturalHeight,
      clientX: e.clientX,
      clientY: e.clientY,
    });
    if (!point) return; // 点在留白区
    interactRef.current = true;
    try {
      await postInteract({ action: 'click', x: point.x, y: point.y });
    } finally {
      setTimeout(() => { interactRef.current = false; }, 300);
    }
  };

  // 画面聚焦后直接敲键盘（英文/数字/退格/回车等）
  const handleStageKeyDown = async (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (!recording || interactRef.current) return;
    if ((e.nativeEvent as unknown as { isComposing?: boolean }).isComposing) return;
    const special = ['Enter', 'Backspace', 'Tab', 'Escape',
      'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'];
    let payload: Record<string, unknown> | null = null;
    if (special.includes(e.key)) payload = { action: 'key', key: e.key };
    else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      payload = { action: 'type', text: e.key };
    }
    if (!payload) return;
    e.preventDefault();
    interactRef.current = true;
    try {
      await postInteract(payload);
    } finally {
      setTimeout(() => { interactRef.current = false; }, 200);
    }
  };

  // 中文/整段文本：用输入框承接输入法组合，回车发送
  const handleSendRecInput = async () => {
    const text = recInput;
    if (!text.trim()) return;
    setRecInput('');
    await postInteract({ action: 'type', text });
  };

  const handleStageWheel = async (e: React.WheelEvent<HTMLDivElement>) => {
    if (!recording || interactRef.current) return;
    interactRef.current = true;
    try {
      await postInteract({ action: 'wheel', dy: Math.round(e.deltaY) });
    } finally {
      setTimeout(() => { interactRef.current = false; }, 250);
    }
  };

  const handleStartRecording = async () => {
    try {
      const res = await fetch(`${AGENT_API_BASE}/browser/record/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name_hint: '' }),
      });
      const data = await res.json();
      if (!res.ok) {
        message.error(data.detail || '开始录制失败');
        return;
      }
      setRecording(data.session);
      setRecordedEvents([]);
      // 录制必须在画面上点击，强制展开到可操作宽度
      setBrowserView('panel');
      setBrowserPanelWidth((w) => Math.max(w, 560));
      // 启动轮询展示已捕获事件
      const timer = setInterval(async () => {
        try {
          const r = await fetch(`${AGENT_API_BASE}/browser/record/peek`);
          if (r.ok) {
            const d = await r.json();
            setRecordedEvents(d.events || []);
          }
        } catch {
          // ignore
        }
      }, 1500);
      setRecordPeekTimer(timer);
      message.success('录制已开始：请在右侧浏览器中手动操作，完成后点击"完成录制"');
    } catch {
      message.error('开始录制失败');
    }
  };

  const handleStopRecording = async () => {
    stopPeekTimer();
    try {
      const res = await fetch(`${AGENT_API_BASE}/browser/record/stop`, { method: 'POST' });
      const data: StopRecordingResult = await res.json();
      if (!res.ok) {
        message.error(data.detail || '停止录制失败');
        setRecording(null);
        return;
      }
      setRecording(null);
      if (data.event_count === 0) {
        message.warning('未捕获到任何操作，已丢弃');
        return;
      }
      // 记录回放命令（供"回放测试"用）
      setLastReplayCommands((data.replay_commands || []).map((c) => c.cmd));
      // 弹出保存应用表单（SOP 预填）
      setSaveAppForm((prev) => ({ ...prev, sop: data.sop }));
      setSaveAppModalOpen(true);
    } catch {
      message.error('停止录制失败');
      setRecording(null);
    }
  };

  const handleCancelRecording = async () => {
    stopPeekTimer();
    try {
      await fetch(`${AGENT_API_BASE}/browser/record/cancel`, { method: 'POST' });
    } catch {
      // ignore
    }
    setRecording(null);
    setRecordedEvents([]);
    message.info('录制已取消');
  };

  const handleSaveRecordedApp = async () => {
    if (!saveAppForm.name.trim()) {
      message.warning('请输入应用名称');
      return;
    }
    if (!saveAppForm.modelKey) {
      message.warning('请选择模型');
      return;
    }
    setSavingApp(true);
    try {
      await agentAppsApi.create({
        name: saveAppForm.name.trim(),
        type: 'COMPUTER_USE',
        status: 'ACTIVE',
        modelKey: saveAppForm.modelKey,
        knowledgeBaseIdsJson: '[]',
        systemPrompt: saveAppForm.sop,
        retrievalConfigJson: '{}',
      });
      // 回放命令存入本地回放库（供"回放测试"）
      if (lastReplayCommands.length > 0) {
        const lib = loadReplayLib();
        lib.push({
          id: `rp_${Date.now()}`,
          name: saveAppForm.name.trim(),
          commands: lastReplayCommands,
          sop: saveAppForm.sop,
          savedAt: new Date().toISOString(),
        });
        saveReplayLib(lib);
      }
      message.success(`应用「${saveAppForm.name.trim()}」创建成功，可在对话中直接下达对应指令使用`);
      setSaveAppModalOpen(false);
      setRecordedEvents([]);
      loadApps(); // 刷新左侧应用侧边栏
    } catch {
      message.error('创建应用失败');
    } finally {
      setSavingApp(false);
    }
  };

  // ============ 确定性回放测试（workflow-use 模式） ============
  const handleReplayTest = async () => {
    if (lastReplayCommands.length === 0) {
      message.warning('没有可回放的命令');
      return;
    }
    setReplayRunning(true);
    setReplayProgress([]);
    setReplaySummary('');
    try {
      const res = await fetch(`${AGENT_API_BASE}/browser/replay`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          commands: lastReplayCommands,
          model: saveAppForm.modelKey || selectedModelKey,
        }),
      });
      if (!res.body) throw new Error('no stream');
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      const progress: typeof replayProgress = [];

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const ev = JSON.parse(line);
            if (ev.type === 'replay_step') {
              const existing = progress.find((p) => p.index === ev.index);
              if (existing) {
                existing.status = ev.status;
                existing.heal = ev.heal;
              } else {
                progress.push({ index: ev.index, total: ev.total, status: ev.status, heal: ev.heal });
              }
              setReplayProgress([...progress]);
            } else if (ev.type === 'replay_done') {
              setReplaySummary(`回放完成：${ev.ok}/${ev.total} 步成功${ev.failed > 0 ? `（${ev.failed} 步失败）` : ''}`);
            } else if (ev.type === 'replay_abort') {
              setReplaySummary(`回放中止：${ev.reason}`);
            } else if (ev.type === 'error') {
              setReplaySummary(`回放错误：${ev.error}`);
            }
          } catch {
            // ignore
          }
        }
      }
    } catch {
      setReplaySummary('回放请求失败');
    } finally {
      setReplayRunning(false);
    }
  };

  // 清理轮询定时器
  useEffect(() => () => stopPeekTimer(), []);

  // 连接状态：有画面=在线；含"中断"=离线；其余=连接中
  const connClass = browserFrame
    ? 'cu-conn-on'
    : browserStatus.includes('中断')
      ? 'cu-conn-off'
      : 'cu-conn-wait';
  const connText = browserFrame ? browserStatus || '实时画面' : browserStatus;

  return (
    <div className="computer-use-page">
      {/* ============ 顶部栏 ============ */}
      <header className="cu-header">
        <div className="cu-header-left">
          <div className="cu-header-icon">
            <DesktopOutlined />
          </div>
          <div>
            <div className="cu-header-title">Computer Use Agent</div>
            <div className="cu-header-sub">对话式浏览器自动化 · 下达指令，Agent 替你操作网页</div>
          </div>
        </div>
        <div className="cu-header-right">
          {recording ? (
            <>
              <span className="cu-rec-badge">
                <i className="cu-rec-dot" />
                录制中 · 已捕获 {recordedEvents.length} 个操作
              </span>
              <Button size="small" onClick={handleCancelRecording}>
                取消
              </Button>
              <Button size="small" type="primary" onClick={handleStopRecording}>
                完成录制
              </Button>
            </>
          ) : (
            <Button icon={<VideoCameraOutlined />} onClick={handleStartRecording}>
              录制新应用
            </Button>
          )}
          <div className={`cu-conn ${connClass}`} title={browserTarget.url || connText}>
            <i className="cu-conn-dot" />
            <span className="cu-conn-text">{connText}</span>
          </div>
          <Select
            placeholder="选择模型"
            value={selectedModelKey}
            onChange={setSelectedModelKey}
            options={models}
            style={{ minWidth: 240 }}
          />
        </div>
      </header>

      {/* ============ 主体：左侧会话 + 中间对话 + 右侧画面（按需展开） ============ */}
      <div className="cu-body">
        {/* 左侧：会话栏（浅色） */}
        <aside className="cu-sidebar">
          <button className="cu-new-btn" onClick={handleNewSession}>
            <PlusOutlined />
            新建对话
          </button>

          {/* 应用速启：预置场景 + 录制的新应用，点击即触发 */}
          <div className="cu-sidebar-label">应用</div>
          <div className="cu-app-list">
            {SCENARIOS.map((s) => (
              <button
                key={`preset-${s.key}`}
                className={`cu-app-item ${loading ? 'cu-app-disabled' : ''}`}
                onClick={() => !loading && handleSend(s.prompt, s.title, s.sop)}
                title={`${s.desc}（${s.difficulty}）`}
              >
                <span className="cu-app-icon">{s.icon}</span>
                <span className="cu-app-info">
                  <span className="cu-app-name">{s.title}</span>
                  <span className="cu-app-tag">预置 · {s.difficulty}</span>
                </span>
              </button>
            ))}
            {customApps.length > 0 && <div className="cu-app-divider" />}
            {customApps.map((a) => (
              <button
                key={a.key}
                className={`cu-app-item cu-app-custom ${loading ? 'cu-app-disabled' : ''}`}
                onClick={() => !loading && handleSend(a.prompt, a.title, a.sop)}
                title={a.desc}
              >
                <span className="cu-app-icon">{a.icon}</span>
                <span className="cu-app-info">
                  <span className="cu-app-name">{a.title}</span>
                  <span className="cu-app-tag">录制的应用</span>
                </span>
              </button>
            ))}
          </div>

          <div className="cu-sidebar-label">历史对话</div>
          <div className="cu-session-list">
            {sessions.length === 0 && (
              <div className="cu-session-empty">
                暂无对话
                <br />
                在下方输入框直接开始
              </div>
            )}
            {sessions.map((s) => (
              <div
                key={s.id}
                className={`cu-session-item ${s.id === activeSessionId ? 'cu-session-active' : ''}`}
                onClick={() => setActiveSessionId(s.id)}
                title={s.title}
              >
                <span className="cu-session-title">{s.title}</span>
                <span className="cu-session-time">{formatTime(s.createdAt)}</span>
              </div>
            ))}
          </div>
          <div className="cu-sidebar-footer">对话仅保存在当前浏览器</div>
        </aside>

        {/* 中间：对话主界面（输入框始终可用，对话优先） */}
        <main className="cu-chat">
          <div className="cu-messages">
            <div className="cu-messages-inner">
              {/* 未开始对话：只保留问候与功能介绍——场景入口在左侧栏，不重复铺卡片 */}
              {!activeSession && (
                <div className="cu-welcome">
                  <div className="cu-welcome-icon">
                    <RobotOutlined />
                  </div>
                  <div className="cu-welcome-title">今天要让我做什么？</div>
                  <div className="cu-welcome-sub">
                    在下方输入框直接下达指令，我会打开浏览器替你完成。
                  </div>
                  <div className="cu-welcome-guide">
                    <div className="cu-guide-item">
                      <b>左侧选择场景</b>
                      <span>港股行情、新股申购、北向资金等 9 个预置应用，点一下即执行</span>
                    </div>
                    <div className="cu-guide-item">
                      <b>直接下指令</b>
                      <span>输入任意网页任务，例如「帮我看下特斯拉今天的股价」</span>
                    </div>
                    <div className="cu-guide-item">
                      <b>录制你的工作流</b>
                      <span>示教一遍常用操作，保存为应用后可反复一键执行</span>
                    </div>
                  </div>
                </div>
              )}

              {/* 对话消息 */}
              {activeSession?.messages.map((msg) =>
                msg.role === 'user' ? (
                  <div key={msg.id} className="cu-msg cu-msg-user">
                    <div className="cu-user-bubble">{msg.content}</div>
                  </div>
                ) : (
                  <div key={msg.id} className="cu-msg cu-msg-agent">
                    <div className="cu-agent-avatar">
                      <RobotOutlined />
                    </div>
                    <div className="cu-agent-col">
                      {/* 执行进度（中文状态） */}
                      <ExecutionProgress steps={msg.toolSteps || []} status={msg.status} />
                      {msg.replayFrames && msg.replayFrames.length > 0 && (
                        <ReplayTimeline frames={msg.replayFrames} />
                      )}

                      {/* 模型思考（去噪、默认折叠） */}
                      <ThinkingBlock raw={msg.thinkingContent} />

                      {/* 回答内容（待执行时不显示空气泡，状态由进度卡片负责） */}
                      {msg.content && (
                        <div
                          className={`cu-agent-bubble ${
                            msg.status === 'error' ? 'cu-agent-error' : ''
                          }`}
                        >
                          <div className="cu-md">
                            <ReactMarkdown rehypePlugins={[rehypeRaw]}>
                              {msg.content}
                            </ReactMarkdown>
                          </div>
                          {msg.status === 'streaming' && <span className="cu-caret" />}
                        </div>
                      )}

                      {msg.status === 'cancelled' && (
                        <div style={{ fontSize: 11, color: '#9198a3' }}>已取消</div>
                      )}
                    </div>
                  </div>
                ),
              )}
              <div ref={messagesEndRef} />
            </div>
          </div>

          {/* 输入区：始终可见、始终可交互（对话式主入口） */}
          <div className="cu-composer-wrap">
            <div className="cu-composer">
              <div className="cu-input-box">
                <TextArea
                  ref={inputRef}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyPress={handleKeyPress}
                  placeholder="输入指令，让 Agent 替你操作网页，例如：查询今天A股涨幅前10…"
                  autoSize={{ minRows: 1, maxRows: 4 }}
                  style={{ flex: 1 }}
                />
                {loading ? (
                  <Button danger icon={<StopOutlined />} className="cu-stop-btn" onClick={handleCancel}>
                    停止
                  </Button>
                ) : (
                  <Button
                    type="primary"
                    icon={<SendOutlined />}
                    disabled={!input.trim()}
                    className="cu-send-btn"
                    onClick={() => handleSend()}
                  >
                    发送
                  </Button>
                )}
              </div>
              <div className="cu-composer-hint">
                <b>Enter</b> 发送 · <b>Shift + Enter</b> 换行
                {loading ? ' · Agent 正在执行，可随时停止' : ''}
              </div>
            </div>
          </div>
        </main>

        {/* 右侧：浏览器实时画面（默认展开，收起时保留醒目入口） */}
        {browserView === 'hidden' ? (
          <aside
            className={`cu-browser-rail ${browserFrame ? 'cu-rail-live' : ''}`}
            title="展开浏览器实时画面"
            onClick={() => setBrowserView('panel')}
          >
            <span className={`cu-rail-dot ${connClass}`} />
            <span className="cu-rail-btn">
              <GlobalOutlined />
            </span>
            <span className={browserFrame ? 'cu-rail-live-text' : 'cu-rail-text'}>
              {browserFrame ? '实时画面 · 点击展开' : '展开实时画面'}
            </span>
          </aside>
        ) : (
          <section
            className="cu-browser"
            style={{ width: browserPanelWidth, minWidth: browserPanelWidth }}
          >
            <div className="cu-browser-head">
              <div className="cu-dots">
                <i />
                <i />
                <i />
              </div>
              <div className="cu-browser-url" title={browserTarget.url || browserTarget.title}>
                <LinkOutlined />
                <span>
                  {browserTarget.url ||
                    browserTarget.title ||
                    (browserFrame ? '等待页面导航…' : '尚未连接浏览器')}
                </span>
              </div>
              {/* 直播状态徽章：帮用户一眼判断画面通道是否正常 */}
              <span
                className={`cu-live-badge ${lastFrameAt ? 'cu-live-on' : ''}`}
                title={
                  lastFrameAt
                    ? `画面通道正常，最近一帧 ${lastFrameAt.toLocaleTimeString()}`
                    : browserStatus || '尚未收到画面帧'
                }
              >
                <i />
                {lastFrameAt
                  ? `直播中 ${lastFrameAt.toLocaleTimeString('zh-CN', { hour12: false })}`
                  : browserStatus || '未连接'}
              </span>
              <div className="cu-browser-actions">
                <div className="cu-size-group">
                  {[380, 560, 800].map((w) => (
                    <button
                      key={w}
                      className={`cu-size-btn ${browserPanelWidth === w ? 'cu-size-active' : ''}`}
                      title={`面板宽度 ${w}px`}
                      onClick={() => setBrowserPanelWidth(w)}
                    >
                      {w === 380 ? '小' : w === 560 ? '中' : '大'}
                    </button>
                  ))}
                </div>
                <button
                  className="cu-browser-collapse-btn"
                  title="全屏放大"
                  onClick={() => setBrowserView('max')}
                >
                  <ExpandOutlined />
                </button>
                <button
                  className="cu-browser-collapse-btn"
                  title="收起面板"
                  onClick={() => setBrowserView('hidden')}
                >
                  <RightOutlined />
                </button>
              </div>
            </div>
            <div
              className={`cu-browser-stage ${recording ? 'cu-stage-interactive' : ''} ${loading && !recording ? 'cu-stage-controlled' : ''}`}
              tabIndex={recording ? 0 : -1}
              onClick={recording ? handleStageClick : undefined}
              onKeyDown={recording ? handleStageKeyDown : undefined}
              onWheel={recording ? handleStageWheel : undefined}
              title={recording ? '录制中：点击/滚轮/直接打字操作浏览器，中文请用右下角输入框' : undefined}
            >
              {browserFrame ? (
                <img src={`data:image/jpeg;base64,${browserFrame}`} alt="浏览器实时画面" />
              ) : null}
              {agentCursor && Date.now() - agentCursor.at < 3000 && (
                <div key={agentCursor.at} className="cu-virtual-cursor">
                  <span className="cu-vc-pointer" />
                  <span className="cu-vc-label">{agentCursor.label}</span>
                </div>
              )}
              {browserFrame ? null : (
                <div className="cu-browser-empty">
                  <GlobalOutlined className="cu-browser-empty-icon" />
                  <div className="cu-browser-empty-text">
                    {browserStatus || '等待 Agent 打开浏览器页面后，这里将显示实时画面'}
                  </div>
                </div>
              )}
              {recording && (
                <div className="cu-stage-cursor-hint">录制中：点击 / 滚轮 / 直接打字；中文用右下角输入框</div>
              )}
              {!recording && loading && (
                <div className="cu-stage-agent-badge">
                  <span className="cu-agent-dot" />
                  Agent 正在操控页面…
                </div>
              )}
            </div>
            {/* 操作轨迹（画面下方的双轨设计：上画面看"它在做什么"，这里看"做到哪步了"。
                任务结束后保留，解决"闪一下就没"——用户可回看每一步操作 */}
            {!recording && activeSession && activeSession.messages.length > 0 && (() => {
              const lastMsg = activeSession.messages[activeSession.messages.length - 1];
              const steps = lastMsg?.toolSteps || [];
              if (steps.length === 0) return null;
              const visible = steps
                .map((s, i) => ({ s, i }))
                .filter(({ s }) => {
                  const d = describeTool(s.name, s.message);
                  return !d.internal;
                });
              if (visible.length === 0) return null;
              const recent = visible.slice(-6);
              return (
                <div className="cu-ops-panel">
                  <div className="cu-ops-head">
                    <ThunderboltOutlined />
                    Agent 操作轨迹
                    <span className="cu-ops-count">{visible.length} 步</span>
                  </div>
                  <ul className="cu-ops-list">
                    {recent.map(({ s, i }) => {
                      const d = describeTool(s.name, s.message);
                      return (
                        <li key={s.id || i} className={`cu-ops-row cu-ops-${s.status}`}>
                          <span className="cu-ops-icon">
                            {s.status === 'running' ? (
                              <LoadingOutlined />
                            ) : s.status === 'error' ? (
                              <span className="cu-ops-retry">↻</span>
                            ) : (
                              <CheckCircleOutlined />
                            )}
                          </span>
                          <span className="cu-ops-label">{d.label}</span>
                          {s.status === 'running' && <span className="cu-ops-tag">执行中</span>}
                          {s.status === 'error' && <span className="cu-ops-tag cu-ops-tag-retry">已重试</span>}
                        </li>
                      );
                    })}
                  </ul>
                </div>
              );
            })()}
          </section>
        )}
      </div>

      {/* ============ 全屏放大模式（覆盖层） ============ */}
      {browserView === 'max' && (
        <div className="cu-max-overlay">
          <div className="cu-max-head">
            <DesktopOutlined style={{ color: '#1677ff', fontSize: 15 }} />
            <span className="cu-max-title">浏览器实时画面</span>
            <div className="cu-browser-url" style={{ flex: 1 }}>
              <LinkOutlined />
              <span>{browserTarget.url || browserTarget.title || '尚未连接浏览器'}</span>
            </div>
            <span className={`cu-rail-dot ${connClass}`} style={{ marginRight: 4 }} />
            <Button size="small" icon={<RightOutlined />} onClick={() => setBrowserView('panel')}>
              还原面板
            </Button>
            <Button size="small" onClick={() => setBrowserView('hidden')}>
              关闭
            </Button>
          </div>
          <div
            className={`cu-max-stage ${recording ? 'cu-stage-interactive' : ''}`}
            tabIndex={recording ? 0 : -1}
            onClick={recording ? handleStageClick : undefined}
            onKeyDown={recording ? handleStageKeyDown : undefined}
            onWheel={recording ? handleStageWheel : undefined}
          >
            {browserFrame ? (
              <img src={`data:image/jpeg;base64,${browserFrame}`} alt="浏览器实时画面（放大）" />
            ) : (
              <div className="cu-browser-empty">
                <GlobalOutlined className="cu-browser-empty-icon" />
                <div className="cu-browser-empty-text">
                  {browserStatus || '等待 Agent 打开浏览器页面后，这里将显示实时画面'}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
      {/* ============ 录制中：实时动作面板（浮层） ============ */}
      {recording && (
        <aside className="cu-rec-panel">
          <div className="cu-rec-head">
            <span className="cu-rec-badge">
              <i className="cu-rec-dot" />
              正在录制你的操作
            </span>
          </div>
          <div className="cu-rec-tip">
            在画面上点击、滚轮或直接打字；中文与整段文字用下方输入框（先点页面里的输入框）
          </div>
          <div className="cu-rec-input-bar">
            <Input
              size="small"
              value={recInput}
              placeholder="输入文字后回车发送（支持中文）"
              onChange={(e) => setRecInput(e.target.value)}
              onPressEnter={handleSendRecInput}
            />
            <Button size="small" icon={<SendOutlined />} onClick={handleSendRecInput}>发送</Button>
            <Button size="small" onClick={() => postInteract({ action: 'key', key: 'Enter' })}>回车</Button>
          </div>
          <div className="cu-rec-events">
            {recordedEvents.length === 0 && (
              <div className="cu-rec-empty">还没有捕获到操作…</div>
            )}
            {recordedEvents.map((e, i) => (
              <div key={i} className="cu-rec-event">
                <span className={`cu-rec-etype cu-rec-${e.type}`}>
                  {e.type === 'click' ? '点击' : e.type === 'fill' ? '输入' : e.type === 'select' ? '选择' : e.type === 'press_enter' ? '回车' : e.type === 'navigate' ? '导航' : e.type === 'scroll' ? '滚动' : e.type === 'key' ? '按键' : e.type}
                </span>
                <span className="cu-rec-detail" title={e.selector}>
                  {e.hint || e.value || e.selector || '-'}
                </span>
              </div>
            ))}
          </div>
          <div className="cu-rec-foot">
            <Button size="small" onClick={handleCancelRecording} style={{ marginRight: 8 }}>
              取消录制
            </Button>
            <Button size="small" type="primary" icon={<CheckCircleOutlined />} onClick={handleStopRecording}>
              完成，保存为应用
            </Button>
          </div>
        </aside>
      )}

      {/* ============ 保存示教应用弹窗 ============ */}
      <Modal
        open={saveAppModalOpen}
        title={
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <VideoCameraOutlined style={{ color: '#1677ff' }} />
            <span>保存为新的 Computer Use 应用</span>
          </div>
        }
        onOk={handleSaveRecordedApp}
        onCancel={() => setSaveAppModalOpen(false)}
        confirmLoading={savingApp}
        okText="创建应用"
        cancelText="取消"
        width={680}
        footer={(_, { OkBtn }) => (
          <>
            {lastReplayCommands.length > 0 && (
              <Button
                key="replay"
                icon={<ThunderboltOutlined />}
                loading={replayRunning}
                onClick={handleReplayTest}
                style={{ float: 'left' }}
              >
                回放测试
              </Button>
            )}
            <Button key="cancel" onClick={() => setSaveAppModalOpen(false)}>
              取消
            </Button>
            <OkBtn />
          </>
        )}
      >
        <div style={{ marginTop: 12 }}>
          {replayProgress.length > 0 && (
            <div
              style={{
                marginBottom: 12,
                padding: '8px 12px',
                background: '#f8f9fb',
                border: '1px solid #e8eaf0',
                borderRadius: 8,
                maxHeight: 160,
                overflowY: 'auto',
                fontSize: 12,
              }}
            >
              {replayProgress.map((p) => (
                <div key={p.index} style={{ padding: '2px 0' }}>
                  步骤 {p.index + 1}:{' '}
                  {p.status === 'success' ? (
                    <span style={{ color: '#389e0d' }}>
                      ✓{p.heal ? `（${p.heal}）` : ''}
                    </span>
                  ) : p.status === 'failed' ? (
                    <span style={{ color: '#cf1322' }}>✗ 失败</span>
                  ) : (
                    <span style={{ color: '#1677ff' }}>执行中…</span>
                  )}
                </div>
              ))}
              {replaySummary && (
                <div style={{ marginTop: 6, fontWeight: 500, color: replaySummary.includes('成功') && !replaySummary.includes('失败') ? '#389e0d' : '#cf1322' }}>
                  {replaySummary}
                </div>
              )}
            </div>
          )}
          <div style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 13, marginBottom: 6, fontWeight: 500 }}>应用名称</div>
            <Input
              placeholder="例如：新股查询助手"
              value={saveAppForm.name}
              onChange={(e) => setSaveAppForm((p) => ({ ...p, name: e.target.value }))}
            />
          </div>
          <div style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 13, marginBottom: 6, fontWeight: 500 }}>执行模型</div>
            <Select
              placeholder="选择模型"
              showSearch
              style={{ width: '100%' }}
              value={saveAppForm.modelKey || undefined}
              onChange={(v) => setSaveAppForm((p) => ({ ...p, modelKey: v }))}
              options={models}
            />
          </div>
          <div>
            <div style={{ fontSize: 13, marginBottom: 6, fontWeight: 500 }}>
              示教的操作步骤（自动生成，可编辑）
            </div>
            <Input.TextArea
              rows={8}
              value={saveAppForm.sop}
              onChange={(e) => setSaveAppForm((p) => ({ ...p, sop: e.target.value }))}
              style={{ fontFamily: 'monospace', fontSize: 12 }}
            />
          </div>
          {lastReplayCommands.length > 0 && (
            <div style={{ marginTop: 8, fontSize: 11, color: '#9198a3' }}>
              点「回放测试」先确定性执行一遍录制的步骤（不调用 LLM，快且省）；失败步骤会自动自愈或由 AI 兜底修复。
            </div>
          )}
        </div>
      </Modal>

    </div>
  );
};

export default AgentComputerUsePage;
