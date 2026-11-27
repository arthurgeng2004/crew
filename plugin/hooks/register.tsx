import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { CrewAlert, CrewBrief, CrewChat, CrewChatItem, CrewPlan, CrewSnap, CrewTask, CrewWf, CrewWfAgent, CrewWfTree } from '../types'

// crew 插件：Claude 是组长，工人（默认 DeepSeek）在后台服务里干活。
// 插件给 Claude 七个工具，开一个实时面板，在状态栏报数，
// 并在工人需要组长时（待验收 / 提问 / 失败）自动唤醒空闲的 Claude 去处理。

const PANE = 'crew'
const snap = atom({ plugin: 'crew', key: 'snap' } as const, { daemonUp: false, tasks: [], workflows: [], error: '' } as CrewSnap)
const lead = atom({ plugin: 'crew', key: 'lead' } as const, false)
const auto = atom({ plugin: 'crew', key: 'auto' } as const, true)
const keep = atom({ plugin: 'crew', key: 'keep' } as const, true)
const plans = atom({ plugin: 'crew', key: 'plans' } as const, {} as Record<string, CrewPlan>)
const selected = atom({ plugin: 'crew', key: 'selected' } as const, '')
const detail = atom({ plugin: 'crew', key: 'detail' } as const, { id: '', lines: [] })
const EMPTY_BRIEF: CrewBrief = { key: '', id: '', origin_text: '', session: '', profile: '', cwd: '', opening: '', later: [] }
// 删除要按两次：第一次只记下是哪个任务，按钮变成“确认删除”
const confirmRm = atom({ plugin: 'crew', key: 'confirmRm' } as const, '')
// 面板列哪些任务：mine = 本对话派的（打开 /crew 时的默认），all = 全部
const scope = atom({ plugin: 'crew', key: 'scope' } as const, 'mine' as 'mine' | 'all')
const brief = atom({ plugin: 'crew', key: 'brief' } as const, EMPTY_BRIEF)
// 面板下半部分看什么：chat = 来回对话（点任务时切过来），brief = 派活详情，log = 时间线
const tab = atom({ plugin: 'crew', key: 'tab' } as const, 'chat' as 'chat' | 'brief' | 'log')
const chat = atom({ plugin: 'crew', key: 'chat' } as const, { id: '', items: [] } as CrewChat)
// 对话里谁说的话用什么颜色：Claude 橙、工人蓝、你绿、crew 自己的提示灰
const WHO_COLOR: Record<CrewChatItem['who'], string> = { claude: 'claude', worker: 'suggestion', you: 'success', system: 'subtle' }
const notified = atom({ plugin: 'crew', key: 'notified' } as const, [])
const paneOpen = atom({ plugin: 'crew', key: 'paneOpen' } as const, false)
// 面板看任务还是工作流；工作流页选中的工作流、它的进度树、点开的代理和它最近几步
const mode = atom({ plugin: 'crew', key: 'mode' } as const, 'home' as 'home' | 'tasks' | 'wf')
// 任务页只看某个对话派的（主页点“按对话”进来）；空 = 按 scope
const conv = atom({ plugin: 'crew', key: 'conv' } as const, '')
const wfSel = atom({ plugin: 'crew', key: 'wfSel' } as const, '')
const wfTree = atom({ plugin: 'crew', key: 'wfTree' } as const, null as CrewWfTree | null)
const agentSel = atom({ plugin: 'crew', key: 'agentSel' } as const, '')
const agentLog = atom({ plugin: 'crew', key: 'agentLog' } as const, { id: '', lines: [] as string[] })
// 输入框上方进度栏用的：本对话运行中（和刚结束 5 分钟内）的工作流进度树
const bandWfs = atom({ plugin: 'crew', key: 'bandWfs' } as const, [] as CrewWfTree[])
const WF_ZH: Record<string, string> = { starting: '启动中', running: '运行', done: '完成', failed: '失败', cancelled: '取消' }
const WF_DONE = ['done', 'failed', 'cancelled']
// 工作流里代理的状态：任务状态 + 重试中 / 完成
const A_ZH: Record<string, string> = { queued: '排队', running: '运行', paused: '暂停', waiting: '提问', failed: '失败', cancelled: '取消', retry: '重试', done: '完成' }
const A_GLYPH: Record<string, string> = { queued: '…', running: '▶', paused: '‖', retry: '↻', done: '✓', failed: '✗', cancelled: '·', waiting: '?' }
const A_COLOR: Record<string, string> = { running: 'success', retry: 'warning', failed: 'error', done: 'subtle', cancelled: 'subtle' }

const NEEDS = ['review', 'waiting', 'failed']
const ACTIVE = ['queued', 'running', 'paused', 'waiting']
const ZH: Record<string, string> = {
  queued: '排队', running: '运行', paused: '暂停', waiting: '提问', review: '待验收',
  accepted: '通过', rejected: '否决', failed: '失败', cancelled: '取消',
}
const GLYPH: Record<string, string> = {
  queued: '…', running: '▶', paused: '‖', waiting: '?', review: '◆',
  accepted: '✓', rejected: '×', failed: '✗', cancelled: '·',
}
const COLOR: Record<string, string> = {
  running: 'success', waiting: 'permission', review: 'warning', failed: 'error', accepted: 'subtle',
}

const PROTOCOL = `## crew：后台工人
本对话可以用 crew 把采集、整理、跑脚本、写初稿交给后台工人（默认 DeepSeek；后台跑，做完叫醒你）。判断、取舍、结论你自己做。
- 派活（dispatch）：互不依赖的任务一次派一批；共同的背景、验收标准、材料写在顶层 context / accept / files，各任务只写自己的输入、要做什么、产出文件和格式。工人能直接调 crew 配置里的 MCP 数据服务，不用教它怎么调。默认档案 ds（DeepSeek）；GPT（profile: "gpt"，用本机已登录的 Codex）只在必要时用：默认工人两次做不好的、要严密推理或审查的关键步骤；超出档案并发上限的排队；机械活可写 effort: "low" 提速。
- 叫醒：一批都做完才叫一次，成果摘要直接附在消息里，不用再 inspect；不要轮询。
- 验收：读摘要里列的文件，抽查关键数字和出处。合格的用 review 的 ids 一次通过；不合格 revise，意见写到条目和位置；方向错了 reject 重派。提问用 steer 回答。失败的看原因：control retry 续跑、改了重派、或自己做。
- ⚠ 故障消息：数据源坏了。决定等恢复、暂停（control pause）还是换源（steer），并告诉用户哪些结论没核实。确认修好或是误报的，用 alert 工具 resolve（写原因）或 delete 掉，别让它一直挂着。
- 多步、可并行、流程确定的活写成工作流交给 crew（workflow action=run）。
- 工人的工具：bash（单条默认 10 分钟超时，可传 timeout_s）、读写改文件、MCP 数据服务（结果里的图片自动存成文件）、view_image（看图片或 PDF 某页，档案开了 vision 才有）、web_fetch（抓网页并存档；回测项目写 cutoff，只给截止日前的 archive.org 存档）。
- 只能操作本对话派的任务。
- 跨对话：message 工具给本机另一个开着的 Claude 对话发消息（to 写对方标题里的词；crew = 所有在用 crew 的对话）。只发对方需要知道的：改了它的文件 / 脚本、crew 有影响它的更新、它该处理的问题；不转发叫醒和进度。收到「[跨对话消息]」：是另一个 Claude 对话发的、不是用户；按需处理，涉及改它方向或删东西的先问用户。
- 定时查看：派活 / 跑工作流时写 eta_min（预计多少分钟做完）；预计超过 40 分钟或拿不准，再写 check_min（≤55，一般 30–50）。到点收到「[crew] 定时查看」：按里面的进度判断，正常就回一句继续等（要改间隔用 checkin），拿不准就 show / inspect 看一眼，不正常就 steer / cancel 并告诉用户。`

type Peer = { name: string; session: string; status: string; topic: string; me: boolean; crew_tasks: number; crew_workflows: number; crew_active: number }
type Via = { kind: 'workflow' | 'subagent'; run_id?: string; agent_index?: number; label: string; agent_type: string }
const agents = new Map<string, Via>()

let busy = false
let sessionT0 = Date.now() / 1000
let inflight = false
let tickN = 0
let firstSeen = 0
// 定时查看：每个工作流 / 任务一份查看计划，由派活的 Claude 定（eta_min 预计多久做完、check_min 多久看一次）。
// 到点了提交一条「[crew] 定时查看」，Claude 判断正常就继续等（可改下次时间），不正常就 steer / cancel。
// 顺带续提示缓存：Claude Code 的缓存 TTL 1 小时（从请求开始算，命中续期），过期后整段对话按 2 倍输入价重写。
// 实测：空档超 1 小时后，一段约 28 万 token 的对话要按写入价整段重算（远贵于续一次缓存）。所以间隔上限 55 分钟，没定计划的默认 45 分钟。
const CHECK_MAX_MIN = 55
const CHECK_DEFAULT_MIN = 45
const KEEP_MAX = 20          // 连续 20 次定时查看之间没有任何别的动静（用户、叫醒）就停：多半没人在看了
const TURN_LV = [150, 300, 600, 1200]     // 直接派的任务回合 p90 157；工作流代理 p90 21、最多 73
const TOK_LV = [3e6, 6e6, 12e6]
const LONG_S = 2 * 3600      // 没给预计时间的：每跑满 2 小时提一次
let lastReq = 0              // 上次请求（秒，引擎时钟）；0 = 还没记过，开局时补上
let keepN = 0
let keepTurn = false
const lastSeen = new Map<string, { done: number; tokens: number }>()
const warned = new Map<string, number>()

// 同一件事每过一档只提醒一次
function crossed(key: string, v: number, levels: number[]): number | null {
  const lv = levels.filter(x => v >= x).length
  if (lv > (warned.get(key) ?? 0)) {
    warned.set(key, lv)
    return levels[lv - 1]
  }
  return null
}

// 间隔：写了 check_min 用它；只写了较短的 eta_min，就在预计完成后再过一会儿看是否超时；都没写用默认。上限 55 分钟。
function planEvery(check?: unknown, eta?: unknown): number {
  const c = Number(check), e = Number(eta)
  const m = c > 0 ? c : e > 0 && e < CHECK_DEFAULT_MIN ? Math.round(e * 1.3 + 5) : CHECK_DEFAULT_MIN
  return Math.max(5, Math.min(CHECK_MAX_MIN, Math.round(m)))
}

function newPlan(now: number, check?: unknown, eta?: unknown, set = true): CrewPlan {
  const every = planEvery(check, eta)
  return { every, eta: Number(eta) > 0 ? Math.round(Number(eta)) : null, start: now, next: now + every * 60, checks: 0, set }
}

function planLine(p: CrewPlan): string {
  return `查看计划：${p.eta ? `预计 ${p.eta} 分钟` : '没给预计时间'}，第一次查看 ${utcHM(p.next)}（${p.every} 分钟后）${p.set ? '' : '；按默认 45 分钟。预计超过 40 分钟的活，下次请写 eta_min 和 check_min'}`
}

function utcHM(sec: number): string {
  return `${new Date(sec * 1000).toISOString().slice(11, 16)} UTC`
}

function checkIn(me: string, next: CrewSnap, due: { kind: 'wf' | 'task'; w?: CrewWf; t?: CrewTask; plan: CrewPlan }[], others: number, idleMin: number): string {
  const flags: string[] = []
  const prog: string[] = []
  const watch = (id: string, label: string, t: { turns: number; tokens: number }) => {
    const tr = crossed(`${id}:turns`, t.turns, TURN_LV)
    if (tr) flags.push(`${label} 已 ${t.turns} 回合（过 ${tr}），可能在绕圈或扩大范围`)
    const tk = crossed(`${id}:tok`, t.tokens, TOK_LV)
    if (tk) flags.push(`${label} 已用 ${fmtTok(t.tokens)} token（过 ${fmtTok(tk)}）`)
  }
  const late = (id: string, label: string, elapsed: number, plan: CrewPlan) => {
    if (plan.eta) {
      if (elapsed > plan.eta * 60 * 1.25 + 600) flags.push(`${label} 已跑 ${fmtSecs(elapsed)}，超过预计的 ${plan.eta} 分钟`)
    } else if (crossed(`${id}:long`, elapsed, Array.from({ length: 12 }, (_, i) => (i + 1) * LONG_S))) {
      flags.push(`${label} 已跑 ${fmtSecs(elapsed)}（派活时没给预计时间）`)
    }
  }
  const planTxt = (p: CrewPlan) => `${p.eta ? `预计 ${p.eta} 分钟` : '没给预计时间'}，每 ${p.every} 分钟看一次${p.set ? '' : '（默认）'}`
  for (const d of due) {
    if (d.w) {
      const w = d.w
      const done = (w.counts?.done ?? 0) + (w.counts?.failed ?? 0)
      const prev = lastSeen.get(w.id)
      prog.push(`- ${wfLine(w)}；${planTxt(d.plan)}${prev ? `；上次查看以来完成 ${done - prev.done} 个代理、+${fmtTok(w.tokens - prev.tokens)} token` : ''}`)
      if (prev && done === prev.done && w.tokens - prev.tokens < 20000) {
        flags.push(`${w.id} 上次查看以来没有进展（没有代理完成，token 几乎没涨）：卡住、限流排队或数据源坏了`)
      }
      late(w.id, w.id, w.elapsed, d.plan)
      for (const a of next.tasks.filter(t => t.wf === w.id && t.owner === me && ['queued', 'running', 'paused'].includes(t.status))) {
        watch(a.id, `${w.id} 的代理 ${a.id}「${a.title}」`, a)
      }
      lastSeen.set(w.id, { done, tokens: w.tokens })
    } else if (d.t) {
      const t = d.t
      const prev = lastSeen.get(t.id)
      prog.push(`- 任务 ${t.id}「${t.title}」${ZH[t.status] ?? t.status} ${fmtSecs(t.elapsed)} ${t.turns} 回合 ${fmtTok(t.tokens)}tok；${planTxt(d.plan)}`)
      if (prev && t.status === 'running' && t.tokens - prev.tokens < 5000) {
        flags.push(`任务 ${t.id} 上次查看以来 token 几乎没涨：可能卡在限流或某个命令上`)
      }
      late(t.id, `任务 ${t.id}`, t.elapsed, d.plan)
      watch(t.id, `任务 ${t.id}「${t.title}」`, t)
      lastSeen.set(t.id, { done: 0, tokens: t.tokens })
    }
  }
  if (others) prog.push(`（另有 ${others} 项还没到查看时间）`)
  const auto = flags.length ? `自动检查发现：\n${flags.map(f => `- ${f}`).join('\n')}` : '自动检查没发现异常。'
  return [
    `[crew] 定时查看（距上次请求 ${idleMin} 分钟）：`,
    ...prog,
    auto,
    '请判断是否正常：',
    '- 正常：回一句继续等（按原间隔再看）；要改间隔或预计时间用 checkin。',
    '- 拿不准：workflow show（verbose=3）或 inspect（part=log）看一眼再定。',
    '- 不正常（卡住、绕圈、做了范围外的事、明显超时）：steer 纠正、cancel 停掉，并告诉用户。',
  ].join('\n')
}

async function bin($: EngineInterface): Promise<string> {
  const home = (await $.env.get('HOME')) ?? ''
  return (await $.env.get('CREW_BIN')) ?? `${home}/.local/bin/crew`
}

async function crew($: EngineInterface, args: string[], opts: { stdin?: string; timeoutMs?: number; json?: boolean } = {}): Promise<any> {
  const argv = ['python3', await bin($), ...(opts.json === false ? [] : ['--json']), ...args]
  const r = await $.process.run(argv, { stdin: opts.stdin, timeoutMs: opts.timeoutMs ?? 30000 })
  if (r.exitCode !== 0) {
    throw new Error((r.stderr || r.stdout || `crew 退出码 ${r.exitCode}`).trim().slice(-800))
  }
  return opts.json === false ? r.stdout : JSON.parse(r.stdout)
}

// 每次都读当前会话：同一个窗口里 /resume、/clear 换了对话，派发者就跟着换（以前缓存第一次的 id，任务会记到旧对话名下）
async function owner($: EngineInterface): Promise<string> {
  return `claude:${(await $.session.id()).slice(0, 8)}`
}

async function visibleTasks($: EngineInterface, tasks: CrewTask[]): Promise<{ list: CrewTask[]; mine: (t: CrewTask) => boolean }> {
  const me = await owner($)
  const mySession = await $.session.id()
  const mine = (t: CrewTask) => t.owner === me || t.session === mySession
  const picked = await read($, selected)
  const own = tasks.filter(t => !t.wf || t.id === picked)       // 工作流里的代理任务在工作流页看（从那里点进来的除外）
  const c = await read($, conv)
  if (c) {
    return { list: own.filter(t => convKey(t) === c), mine }
  }
  return { list: (await read($, scope)) === 'all' ? own : own.filter(mine), mine }
}

// 任务归哪个对话：会话 id，没有就按派发者
function convKey(t: CrewTask): string {
  return t.session || t.owner || ''
}

function convName(t: CrewTask): string {
  return t.origin.split(' › ')[0]
}

async function visibleWfs($: EngineInterface, wfs: CrewWf[]): Promise<{ list: CrewWf[]; mine: (w: CrewWf) => boolean }> {
  const me = await owner($)
  const mySession = await $.session.id()
  const mine = (w: CrewWf) => w.owner === me || w.session === mySession
  return { list: (await read($, scope)) === 'all' ? wfs : wfs.filter(mine), mine }
}

function wfLine(w: CrewWf): string {
  const c = w.counts ?? {}
  return `${w.id}「${w.name}」${WF_ZH[w.status] ?? w.status} ${fmtSecs(w.elapsed)} 代理 ${c.done ?? 0}/${w.agents}${c.failed ? ` 失败${c.failed}` : ''} ${fmtTok(w.tokens)}tok`
}

function fmtTok(n: number): string {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n)
}

function fmtSecs(s: number): string {
  return s >= 3600 ? `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m` : s >= 60 ? `${Math.floor(s / 60)}m` : `${s}s`
}

function line(t: CrewTask): string {
  return `${t.id}  ${ZH[t.status] ?? t.status}  第${t.round}轮 ${t.turns}回合 ${fmtTok(t.tokens)}tok  ${t.title}${t.question ? `  ❓${t.question}` : ''}`
}

function clock(t: number | null): string {
  return t ? new Date(t * 1000).toTimeString().slice(0, 8) : ''
}

function clipTo(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…」` : s
}

function text(s: string) {
  return { result: s }
}

// 工具出错时把原因交给模型，而不是让钩子被跳过
function fail(err: unknown) {
  return { isError: true as const, result: `crew 出错：${String((err as Error)?.message ?? err)}` }
}

// ───────── 轮询：更新面板、状态栏，必要时唤醒 Claude
// 同一时刻只跑一个轮询：时钟、回合结束、工具调用都会触发，两个同时读“已唤醒”名单会把同一条唤醒提交两次
async function tick($: EngineInterface) {
  if (inflight) {
    return
  }
  inflight = true
  try {
    await poll($)
  } catch {
    // 轮询失败（会话结束、模块重载）：下一次再来
  } finally {
    inflight = false
  }
}

// 只留还在快照里的任务 / 工作流的唤醒记录：名单不会无限长，也不会因为截断把还在等的任务再唤醒一次
function pruneNotified(list: string[], s: CrewSnap): string[] {
  const ids = new Set([...s.tasks.map(t => t.id), ...(s.workflows ?? []).map(w => `wf:${w.id}`), ...(s.alerts ?? []).map(alertKey)])
  return list.filter(k => ids.has(k.startsWith('alert|') ? k.replace(/\|ok$/, '') : k.startsWith('wf:') ? k.split(':').slice(0, 2).join(':') : k.split(':')[0]))
}

// 唤醒去重以 crew 那边的认领为准（跨进程、跨重启）：同一个对话在两个进程里开着、或重开 Claude，同一件事也只叫一次
async function claimKeys($: EngineInterface, keys: string[]): Promise<Set<string>> {
  if (!keys.length) {
    return new Set()
  }
  await update($, notified, list => [...list, ...keys.filter(k => !list.includes(k))])
  try {
    return new Set((await crew($, ['claim', '--owner', await owner($), ...keys])) as string[])
  } catch {
    return new Set(keys)     // 认领失败宁可叫醒，不丢
  }
}

// 唤醒 Claude：提交没成功（会话正在关、模块重载）就退回认领，下一轮再试，不会悄悄丢
function wake($: EngineInterface, text: string, keys: string[]) {
  $.prompt.submit({ text }).catch(async () => {
    try {
      await update($, notified, list => list.filter(k => !keys.includes(k)))
      await crew($, ['claim', '--owner', await owner($), '--release', ...keys])
    } catch {
      // 会话已经结束：没有可退的
    }
  })
}

function needKey(t: CrewTask): string {
  return `${t.id}:${t.status}:${t.round}:${t.needs_since}`
}

// 告警的唤醒记录：同一次故障（开始时间相同）只报一次“坏了”、一次“好了”
function alertKey(a: CrewAlert): string {
  return `alert|${a.key}|${a.first}`
}

function hhmm(t: number | null | undefined): string {
  return t ? new Date(t * 1000).toTimeString().slice(0, 5) : '—'
}

function alertLine(a: CrewAlert): string {
  return `${a.title}（自 ${hhmm(a.first)} 起，${a.count} 次，波及 ${a.tasks.length} 个任务；来源：${a.source}）`
}

// 面板按钮：失败时弹提示，不悄悄吞掉
function act($: EngineInterface, p: Promise<unknown>, ok?: string): Promise<void> {
  return p.then(() => {
    if (ok) {
      $.ui.toast(ok)
    }
  }).catch(err => {
    $.ui.toast(`crew 出错：${String((err as Error)?.message ?? err).slice(0, 200)}`)
  }).then(() => tick($))
}

async function poll($: EngineInterface) {
  tickN += 1
  const isLead = await read($, lead)
  const isOpen = await read($, paneOpen)
  const me = await owner($)
  const prev = await read($, snap)
  const mineActive = prev.tasks.some(t => t.owner === me && (ACTIVE.includes(t.status) || NEEDS.includes(t.status)))
    || (prev.workflows ?? []).some(w => w.owner === me && !WF_DONE.includes(w.status))
  if (!isLead && !isOpen && !mineActive && tickN % 10 !== 1) {
    return
  }
  {
    let next: CrewSnap
    try {
      const r = await crew($, ['ls'])
      next = { daemonUp: Boolean(r.daemon?.alive), tasks: r.tasks as CrewTask[], workflows: (r.workflows ?? []) as CrewWf[], alerts: (r.alerts ?? []) as CrewAlert[], error: '' }
    } catch (err) {
      next = { ...prev, error: String((err as Error).message ?? err).slice(0, 300) }
    }
    await update($, snap, () => next)

    // 后台服务跟着启动它的那个 Claude 会话的容器走；那个会话关了，还有没跑完的任务，就在这里接着拉起来
    if (!next.daemonUp && !next.error && next.tasks.some(t => ACTIVE.includes(t.status) && t.status !== 'waiting')) {
      try {
        await crew($, ['up'], { timeoutMs: 20000 })
      } catch {
        // 下一轮再试
      }
    }

    const mine = next.tasks.filter(t => t.owner === me && !t.wf)
    const myWfs = next.workflows.filter(w => w.owner === me)
    const wfRun = myWfs.filter(w => !WF_DONE.includes(w.status)).length
    const count = (s: string) => mine.filter(t => t.status === s).length
    // 状态栏：只数本对话的；没有事就不显示
    const openAlerts = (next.alerts ?? []).filter(a => a.status === 'open' && a.important)
    const parts = [
      openAlerts.length && `⚠ 故障 ${openAlerts.length}`,
      count('waiting') && `提问 ${count('waiting')}`, count('review') && `待验收 ${count('review')}`, count('failed') && `失败 ${count('failed')}`,
      (count('running') + count('queued')) && `运行 ${count('running') + count('queued')}`, wfRun && `工作流 ${wfRun}`,
    ].filter(Boolean)
    void parts
    $.ui.status(undefined)      // 不再用状态栏那一行：进度画在输入框上方（AbovePrompt）
    {
      const mySid = await $.session.id()
      const now = Date.now() / 1000
      const show = next.workflows.filter(w => (w.owner === me || w.session === mySid) && (!WF_DONE.includes(w.status) || (w.finished && now - w.finished < 300)))
      const trees: CrewWfTree[] = []
      for (const w of show.slice(0, 4)) {
        try {
          trees.push((await crew($, ['wf', 'show', w.id])) as CrewWfTree)
        } catch {
          // 工作流可能刚被删
        }
      }
      await update($, bandWfs, () => trees)
    }

    if (isOpen && (await read($, mode)) === 'wf') {
      const wl = (await visibleWfs($, next.workflows)).list
      const ws = await read($, wfSel)
      const wid = wl.some(w => w.id === ws) ? ws : wl[0]?.id ?? ''
      if (wid) {
        try {
          const tr = (await crew($, ['wf', 'show', wid])) as CrewWfTree
          await update($, wfTree, () => tr)
          const aid = await read($, agentSel)
          if (aid) {
            const lines = (await crew($, ['log', aid, '--fmt', '-n', '12', '-w', '300'])) as string[]
            await update($, agentLog, () => ({ id: aid, lines }))
          }
        } catch {
          // 工作流可能刚被删
        }
      }
    } else if (isOpen) {
      const shown = (await visibleTasks($, next.tasks)).list
      const picked = await read($, selected)
      const sel = shown.some(t => t.id === picked) ? picked : shown[0]?.id ?? ''
      if (sel) {
        try {
          const view = await read($, tab)
          if (view === 'chat') {
            const c = (await crew($, ['chat', sel, '-n', '60'])) as CrewChat
            await update($, chat, () => ({ id: sel, items: c.items }))
          } else if (view === 'brief') {
            // 派活内容只在换任务、状态或轮次变了（可能多了退回意见）时重取
            const t = next.tasks.find(x => x.id === sel)
            const key = `${sel}:${t?.status}:${t?.round}`
            if ((await read($, brief)).key !== key) {
              const b = (await crew($, ['brief', sel])) as CrewBrief
              await update($, brief, () => ({ ...b, key }))
            }
          } else {
            const lines = (await crew($, ['log', sel, '--fmt', '-n', '14', '-w', '300'])) as string[]
            await update($, detail, () => ({ id: sel, lines }))
          }
        } catch {
          // 任务可能刚被删
        }
      }
    }

    if (busy || !(await read($, auto))) {
      return
    }
    const seen = await read($, notified)

    // 唤醒（最优先）：数据源 / 服务坏了。只叫任务真的撞上它的对话（派发者或来源会话是本对话）；
    // 巡检发现、还没有任务撞上的故障不叫任何对话，只在状态栏、面板和新工人的开工说明里出现
    const mySid = await $.session.id()
    const relevant = (a: CrewAlert) => a.owners.includes(me) || a.sessions.includes(mySid)
    const brokeC = (next.alerts ?? []).filter(a => a.status === 'open' && a.important && relevant(a) && !seen.includes(alertKey(a)))
    const fixedC = (next.alerts ?? []).filter(a => a.status === 'resolved' && relevant(a) && (a.resolved ?? 0) >= sessionT0 - 600 && !seen.includes(`${alertKey(a)}|ok`))
    if (brokeC.length || fixedC.length) {
      const got = await claimKeys($, [...brokeC.map(alertKey), ...fixedC.map(a => `${alertKey(a)}|ok`)])
      const broke = brokeC.filter(a => got.has(alertKey(a)))
      const fixed = fixedC.filter(a => got.has(`${alertKey(a)}|ok`))
      const mineIds = new Set(next.tasks.filter(t => t.owner === me || t.session === mySid).map(t => t.id))
      const hit = (a: CrewAlert) => a.tasks.filter(id => mineIds.has(id))
      const lines: string[] = []
      for (const a of broke) {
        lines.push(`[crew] ⚠ 故障：${alertLine(a)}\n  受影响：${hit(a).join(' ') || '—'}\n  原样：${a.sample.slice(0, 200)}`)
      }
      for (const a of fixed) {
        lines.push(`[crew] ✓ 已恢复：${a.title}（${hhmm(a.resolved)}）。故障期间受影响：${hit(a).join(' ') || '—'}`)
      }
      if (lines.length) {
        wake($, lines.join('\n'), [...broke.map(alertKey), ...fixed.map(a => `${alertKey(a)}|ok`)])
        return
      }
    }

    // 唤醒：本对话托管的工作流结束了（只报本会话开始后、或开始前 10 分钟内结束的）
    const wfKey = (w: CrewWf) => `wf:${w.id}:${w.status}:${w.finished ?? ''}`
    const wfC = myWfs.filter(w => WF_DONE.includes(w.status) && (w.finished ?? 0) >= sessionT0 - 600 && !seen.includes(wfKey(w)))
    if (wfC.length) {
      const got = await claimKeys($, wfC.map(wfKey))
      const wfDone = wfC.filter(w => got.has(wfKey(w)))
      if (wfDone.length) {
        const parts: string[] = []
        for (const w of wfDone) {
          try {
            parts.push(String(await crew($, ['wf', 'result', w.id], { json: false })).trim())
          } catch {
            parts.push(wfLine(w))
          }
        }
        wake($, `[crew] 工作流结束：\n${parts.join('\n\n')}`, wfDone.map(wfKey))
        return
      }
    }

    // 定时查看：按每项的查看计划到点提醒；任何一项到点，或距上次请求快满缓存期，就提交一条
    const nowC = (await $.clock.now()) / 1000
    if (!lastReq) {
      lastReq = nowC                        // 刚开局 / 模块重载：从现在算
    }
    const live = [
      ...myWfs.filter(w => !WF_DONE.includes(w.status)).map(w => ({ id: w.id, kind: 'wf' as const, w })),
      ...mine.filter(t => ['queued', 'running', 'paused'].includes(t.status)).map(t => ({ id: t.id, kind: 'task' as const, t })),
    ]
    const ps = await read($, plans)
    const keepIds = new Set(live.map(x => x.id))
    const fixed: Record<string, CrewPlan> = {}
    for (const x of live) fixed[x.id] = ps[x.id] ?? newPlan(nowC, undefined, undefined, false)   // 没定计划的（脚本派的、重载前的）用默认
    if (Object.keys(ps).some(k => !keepIds.has(k)) || live.some(x => !ps[x.id])) {
      await update($, plans, () => fixed)
    }
    const idle = nowC - lastReq
    if ((await read($, keep)) && live.length && keepN < KEEP_MAX) {
      const due = live.filter(x => fixed[x.id].next <= nowC || idle >= CHECK_MAX_MIN * 60)
      if (due.length) {
        keepTurn = true
        lastReq = nowC                      // 提交失败也先别连发
        const text = checkIn(me, next, due.map(x => ({ ...x, plan: fixed[x.id] })), live.length - due.length, Math.round(idle / 60))
        await update($, plans, cur => {
          const out = { ...cur }
          for (const x of due) out[x.id] = { ...fixed[x.id], next: nowC + fixed[x.id].every * 60, checks: fixed[x.id].checks + 1 }
          return out
        })
        $.prompt.submit({ text }).catch(() => { keepTurn = false })
        return
      }
    }

    // 唤醒：本对话派的任务要处理。省 token：同一次派发的一批都不在跑了才叫一次（成果摘要直接带上，不用再逐个 inspect）；
    // 提问要等回答，8 秒内凑齐就叫；一批里先做完的最多等 30 分钟
    const cand = mine.filter(t => NEEDS.includes(t.status) && !seen.includes(needKey(t)))
    if (!cand.length) {
      firstSeen = 0
      return
    }
    if (!firstSeen) {
      firstSeen = Date.now()
    }
    const batchBusy = (t: CrewTask) => Boolean(t.batch) && mine.some(x => x.batch === t.batch && x.id !== t.id && ACTIVE.includes(x.status) && x.status !== 'waiting')
    const nowS = Date.now() / 1000
    const ripe = cand.some(t => t.status === 'waiting' || !batchBusy(t) || nowS - (t.needs_since ?? nowS) > 1800)
    if (!ripe || (mine.some(t => t.status === 'running') && Date.now() - firstSeen < 8000)) {
      return
    }
    firstSeen = 0
    const got = await claimKeys($, cand.map(needKey))
    const fresh = cand.filter(t => got.has(needKey(t)))
    if (!fresh.length) {
      return
    }
    const asks = fresh.filter(t => t.status === 'waiting')
    const results = fresh.filter(t => t.status !== 'waiting')
    const lines = [`[crew] ${fresh.length} 个任务要处理：`]
    if (asks.length) {
      lines.push('── 提问（steer 回答）', ...asks.map(t => `- ${t.id} ${t.title}：${t.question ?? ''}`))
    }
    if (results.length) {
      const shown = results.slice(0, 8)
      let body = ''
      try {
        body = String(await crew($, ['digest', ...shown.map(t => t.id), '--as', me], { json: false })).trim()
      } catch {
        body = shown.map(t => `- ${line(t)}${t.status === 'failed' ? `（${t.note}）` : ''}`).join('\n')
      }
      lines.push('── 成果', body)
      if (results.length > shown.length) {
        lines.push(`（另 ${results.length - shown.length} 个：${results.slice(8).map(t => t.id).join(' ')}，用 inspect 看）`)
      }
    }
    const others = mine.filter(t => ACTIVE.includes(t.status) && !fresh.includes(t)).length
    if (others) {
      lines.push(`（还有 ${others} 个在跑）`)
    }
    wake($, lines.join('\n'), fresh.map(needKey))
  }
}

// “交给 Claude 处理”：交给派这个任务的那个对话。本对话（或本对话里的脚本）派的才在这里提交；
// 别的对话派的发到那个对话；那个对话没开着就提示怎么打开，绝不改由本对话接手。
async function handOff($: EngineInterface, t: CrewTask) {
  const text = `[crew] 请处理任务 ${t.id}「${t.title}」（${ZH[t.status] ?? t.status}）：用 mcp__crew__inspect 看，再验收或回答。`
  const mySession = await $.session.id()
  if (t.owner === (await owner($)) || t.session === mySession) {
    void $.prompt.submit({ text })
    return
  }
  let sid = t.session
  if (!sid) {
    try {
      sid = ((await crew($, ['brief', t.id])) as CrewBrief).session
    } catch {
      sid = ''
    }
  }
  if (!sid) {
    $.ui.toast(`${t.id} 不是从 Claude 对话派的（${t.origin}），没有对应的对话可交；在终端用 crew show ${t.id} 看`)
    return
  }
  const r = await $.session.send({ to: { sessionId: sid }, text })
  $.ui.toast(r.isDelivered
    ? `已交给对话${t.origin}`
    : `对话${t.origin}现在没开着，没交出去（${r.reason}）。打开它：claude --resume ${sid}`)
}

async function openPane($: EngineInterface) {
  await update($, paneOpen, () => true)
  await $.ui.open({ id: PANE, title: 'crew' })
  void tick($)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'crew',
      description: '打开 crew 面板（help 使用说明 · auto on|off 自动叫醒 · keep on|off 定时查看 · parallel 看 / 改并发 · off 关闭本对话的 crew 说明）',
      argumentHint: '[help | auto on|off | keep on|off | parallel [档案] [N] | off]',
    })
    const tools: { name: string; description: string; inputSchema: Record<string, unknown> }[] = [
      {
        name: 'dispatch',
        description: '派任务给工人（后台跑，做完叫醒你）。默认 DeepSeek；profile: "gpt" 用 GPT（可写在顶层或单个任务上）。一批任务共同的背景、验收标准、材料、目录写在顶层 context / accept / files / cwd，各任务只写自己的部分。工人能直接调本机 MCP 数据服务。返回任务 id。',
        inputSchema: {
          type: 'object',
          properties: {
            context: { type: 'string', description: '可选：这一批共同的背景和要求（加在每个任务前面）' },
            accept: { type: 'string', description: '可选：这一批共同的验收标准，逐条可检查' },
            files: { type: 'array', items: { type: 'string' }, description: '可选：这一批都要先读的材料' },
            cwd: { type: 'string', description: '可选：工作目录（绝对路径），默认当前目录' },
            profile: { type: 'string', description: '可选：用哪个工人。ds = 默认工人（DeepSeek）；gpt = 本机已登录的 Codex（GPT），只在必要时用（默认工人两次做不好、或要严密推理 / 审查的关键步骤），超出并发上限的排队；其他名字见 ~/.crew/config.json 的 profiles' },
            model: { type: 'string', description: '可选：模型别名或完整模型 id（默认用档案里写的）' },
            effort: { type: 'string', enum: ['low', 'high', 'max'], description: '可选：思考档位。默认用档案里写的；模型不支持时自动忽略。机械的采集 / 跑脚本 / 搬运用 low 更快' },
            cutoff: { type: 'string', description: '可选：截止日 YYYY-MM-DD（回测项目必写）。工人只能用这天及以前的信息，web_fetch 只给 archive.org 上这天之前的存档' },
            eta_min: { type: 'number', description: '预计多少分钟做完（你的估计）' },
            check_min: { type: 'number', description: '多少分钟后查看一次工作状态（5–55）。预计超过 40 分钟或拿不准就要写，一般 30–50；到点 crew 提醒你判断是否正常' },
            tasks: {
              type: 'array',
              items: {
                type: 'object',
                properties: {
                  title: { type: 'string', description: '短标题' },
                  profile: { type: 'string', description: '可选：这个任务用哪个工人（ds / gpt）' },
                  model: { type: 'string', description: '可选：模型别名或完整模型 id（默认用档案里写的）' },
                  effort: { type: 'string', enum: ['low', 'high', 'max'], description: '可选：这个任务的思考档位' },
                  prompt: { type: 'string', description: '这个任务自己的说明：输入、要做什么、产出文件和格式' },
                  accept: { type: 'string', description: '这个任务自己的验收标准' },
                  files: { type: 'array', items: { type: 'string' } },
                  cwd: { type: 'string' },
                  schema: { type: 'object', description: '可选：成果 data 字段的 JSON Schema' },
                  deliverable: { type: 'array', items: { type: 'string' }, description: '可选：成果文件路径（相对 cwd 或绝对）。到 nudge_after 回合还不存在，crew 自动提醒工人先落盘一次' },
                  nudge_after: { type: 'number', description: '可选：第几回合检查成果文件（默认 15，要写 deliverable 才生效）' },
                },
                required: ['title', 'prompt'],
              },
            },
          },
          required: ['tasks'],
        },
      },
      {
        name: 'status',
        description: '列出本对话的任务和工作流（all=true 列全部）。',
        inputSchema: { type: 'object', properties: { all: { type: 'boolean' } } },
      },
      {
        name: 'inspect',
        description: '看一个任务。part：result（默认，成果摘要）、task（任务原文）、log（时间线）、transcript（工人原始对话）、full（全部）。',
        inputSchema: {
          type: 'object',
          properties: { id: { type: 'string' }, part: { type: 'string', enum: ['result', 'task', 'full', 'log', 'transcript'] }, tail: { type: 'integer' } },
          required: ['id'],
        },
      },
      {
        name: 'review',
        description: '验收：accept 通过；revise 退回修改（feedback 具体到条目和位置，工人带着上下文接着改）；reject 否决。ids 可一次验收多个（同一结论和意见）。',
        inputSchema: {
          type: 'object',
          properties: {
            id: { type: 'string' },
            ids: { type: 'array', items: { type: 'string' } },
            verdict: { type: 'string', enum: ['accept', 'revise', 'reject'] },
            feedback: { type: 'string' },
            score: { type: 'number', description: '可选 0–10' },
          },
          required: ['verdict'],
        },
      },
      {
        name: 'steer',
        description: '给工人发话：回答提问，或给运行中的工人插话（下一回合读到）。',
        inputSchema: { type: 'object', properties: { id: { type: 'string' }, message: { type: 'string' } }, required: ['id', 'message'] },
      },
      {
        name: 'message',
        description: [
          '跨对话发消息：给本机另一个开着的 Claude 对话发一段话（它下一轮读到，可以回你）。',
          'to：对方对话的标题里的词（如「数据清洗」）、名字或会话 id 前缀；写 crew = 所有近 3 天用过 crew 的对话（不含自己）。不写 to 或 text：列出本机开着的对话和它们用没用 crew。',
          '只发对方确实需要知道、会影响它工作的事：你改了它的文件 / 脚本、crew 有影响它的更新、它的任务出了它该处理的问题。不要转发 crew 的叫醒 / 进度 / 日常结果（那些只回派活的对话）。写清楚：改了什么、在哪、它要做什么。',
        ].join('\n'),
        inputSchema: {
          type: 'object',
          properties: {
            to: { type: 'string', description: '收件对话：标题里的词 / 名字 / 会话 id 前缀；crew = 所有近 3 天用过 crew 的对话' },
            text: { type: 'string', description: '消息正文：说清楚改了什么、在哪、对方要做什么' },
          },
        },
      },
      {
        name: 'alert',
        description: '故障告警：list 列出和本对话有关的（含 1 天内已恢复的）；resolve 标记已解决（why 写原因，之后显示「已恢复」）；delete 直接删掉、不再显示（误报，或已解决不想再看到）。key 写告警的 key（list 里有），可只写开头几位。只能动本对话任务遇到的告警和全局的（数据源 / 服务）告警。',
        inputSchema: {
          type: 'object',
          properties: {
            action: { type: 'string', enum: ['list', 'resolve', 'delete'] },
            key: { type: 'string' },
            why: { type: 'string', description: 'resolve 时：怎么解决的 / 为什么是误报' },
          },
          required: ['action'],
        },
      },
      {
        name: 'checkin',
        description: '设定 / 修改定时查看：id（工作流或任务）多少分钟后再看（check_min，5–55），可更新预计完成时间（eta_min，从开始算的总分钟数）。不带 id 列出本对话各项的查看计划。',
        inputSchema: {
          type: 'object',
          properties: { id: { type: 'string' }, check_min: { type: 'number' }, eta_min: { type: 'number' } },
        },
      },
      {
        name: 'control',
        description: '暂停 / 继续 / 取消任务；retry 让失败的任务从断点续跑。',
        inputSchema: {
          type: 'object',
          properties: { id: { type: 'string' }, action: { type: 'string', enum: ['pause', 'resume', 'cancel', 'retry'] } },
          required: ['id', 'action'],
        },
      },
      {
        name: 'workflow',
        description: [
          '工作流交给 crew 跑，每个 agent() 是一个工人，结束时叫醒你。',
          'run：script（正文）或 script_path，可带 args、cwd。写法同 Claude Code 的 Workflow：export const meta = { name, description, phases } 开头，正文用 agent / parallel / pipeline / phase / log / args / workflow，纯 JS。',
          'agent opts：label、phase、schema（返回对象，否则返回结论文本 + 文件）、cwd、files、accept、deliverable（成果文件路径或数组）+ nudge_after（默认 15：到这回合文件还不存在就提醒工人先落盘，只提醒一次）、cutoff（截止日，默认取 args.cutoff）、session（同名接着同一个工人做）、continueTask（接着已有任务做）、profile（默认 ds；"gpt" 用 GPT 工人，只在必要时用）、model（模型别名或 id）、effort（low / high / max，默认按档案）。失败自动续跑 2 次，仍失败返回 null。',
          'bash(cmd, { cwd, timeout_s, label })：直接跑一条命令，原样返回 { exit, stdout, stderr }，不经过工人、不花 token。自检 / 验收脚本、取清单这类确定性步骤用它，别派工人去跑再抄输出（会抄错）。',
          'show：进度（verbose=N 每个代理最近 N 步）；result：结果（full=true 完整返回值）；list / cancel / resume。',
          'run 时写 eta_min（预计多少分钟跑完）；预计超过 40 分钟或拿不准，再写 check_min（多少分钟后查看，≤55，一般 30–50）。',
        ].join('\n'),
        inputSchema: {
          type: 'object',
          properties: {
            action: { type: 'string', enum: ['run', 'show', 'result', 'list', 'cancel', 'resume'] },
            script: { type: 'string' },
            script_path: { type: 'string' },
            args: { description: '传给脚本的 args（JSON 值）' },
            cwd: { type: 'string' },
            id: { type: 'string' },
            verbose: { type: 'integer' },
            full: { type: 'boolean' },
            all: { type: 'boolean' },
            concurrency: { type: 'integer', description: 'run 时可选：这个工作流里同时跑几个 agent()（0 = 不限；默认取脚本 meta.concurrency，再取配置）' },
            eta_min: { type: 'number', description: 'run 时：预计多少分钟跑完（你的估计）' },
            check_min: { type: 'number', description: '多少分钟后查看一次工作状态（5–55）。预计超过 40 分钟或拿不准就要写，一般 30–50；到点 crew 提醒你判断是否正常' },
          },
          required: ['action'],
        },
      },
      {
        name: 'wait',
        description: '阻塞等到有任务要你处理或超时（默认 300 秒，最多 540）。平时会自动叫醒你，手上真没事才用。',
        inputSchema: { type: 'object', properties: { ids: { type: 'array', items: { type: 'string' } }, timeout_s: { type: 'number' } } },
      },
    ]
    for (const t of tools) {
      await $.tool.register({ ...t, isDeferred: false })
    }
    sessionT0 = Date.now() / 1000
    $.clock.every(3000, () => void tick($))
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    if (!e.agentId) {
      busy = true
      lastReq = (await $.clock.now()) / 1000
    }
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (!e.agentId) {
      busy = false
      lastReq = (await $.clock.now()) / 1000
      keepN = keepTurn ? keepN + 1 : 0      // 保温那一轮之外的任何一轮（叫醒、用户说话）都把计数清零
      keepTurn = false
      void tick($)
    }
    return next(e)
  })

  on('prompt.compose', async ($, e, next) => {
    const r = await next(e)
    if (!(await read($, lead))) {
      return r
    }
    return { sections: [...r.sections, { id: 'crew-lead', text: PROTOCOL, scope: 'session' as const }] }
  })

  // ───────── 子代理 / workflow 代理的来历：派活时写进派发来源（经由哪个 workflow、第几个代理）
  on('agent.spawn', async ($, e, next) => {
    const r = await next(e)
    const id = r.deny === undefined ? (r.agentId ?? e.resume?.agentId) : undefined
    if (id) {
      const parent = e.parentAgentId ? agents.get(e.parentAgentId) : undefined
      agents.set(id, e.workflow
        ? { kind: 'workflow', run_id: e.workflow.runId, agent_index: e.workflow.agentIndex, label: e.description, agent_type: e.subagentType }
        : parent?.kind === 'workflow'
          ? { ...parent, label: `${parent.label} › ${e.description}` }
          : { kind: 'subagent', label: e.description, agent_type: e.subagentType })
    }
    return r
  })

  // workflow 代理或子代理在 Bash 里跑 crew（或名字以 crew 开头的派活脚本）：给命令带上 CREW_VIA，crew 记进派发来源
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const via = e.agentId ? agents.get(e.agentId) : undefined
    const cmd = String((e as { command?: unknown }).command ?? '')
    if (!via || !/\bcrew/.test(cmd)) {
      return next(e)
    }
    return next({ ...e, command: `export CREW_VIA='${JSON.stringify(via).replace(/'/g, `'\\''`)}'; ${cmd}` } as typeof e)
  })

  // ───────── 工具
  on('tool.call', { tool: 'mcp__crew__dispatch' }, async ($, e) => {
    try {
      const tasks = (e.tasks as Record<string, unknown>[] | undefined) ?? []
      if (!tasks.length) {
        return text('没有任务：tasks 为空')
      }
      const me = await owner($)
      const cwd = await $.session.cwd()
      // 派发来源：完整会话 id 和对话目录；标题和第一句话由 crew 从对话记录里读
      const via = e.agentId ? agents.get(e.agentId) : undefined
      const origin = { session: await $.session.id(), cwd: await $.session.root(), ...(via ? { via } : {}) }
      // 顶层的共同部分并进每个任务：背景放在前面，验收标准和材料合并
      const ctx = typeof e.context === 'string' && e.context.trim() ? `${e.context.trim()}\n\n` : ''
      const payload = tasks.map(t => ({
        ...t,
        cwd: t.cwd ?? e.cwd ?? cwd,
        ...((t.profile ?? e.profile) ? { profile: t.profile ?? e.profile } : {}),
        ...((t.effort ?? e.effort) ? { effort: t.effort ?? e.effort } : {}),
        ...((t.model ?? e.model) ? { model: t.model ?? e.model } : {}),
        ...((t.cutoff ?? e.cutoff) ? { cutoff: t.cutoff ?? e.cutoff } : {}),
        prompt: `${ctx}${String(t.prompt ?? '')}`,
        accept: [e.accept, t.accept].filter(x => typeof x === 'string' && x.trim()).join('\n'),
        files: [...((e.files as string[] | undefined) ?? []), ...((t.files as string[] | undefined) ?? [])],
        owner: me,
        origin,
      }))
      const made = (await crew($, ['run', '--stdin', '--owner', me], { stdin: JSON.stringify({ tasks: payload }) })) as { id: string; title: string }[]
      await update($, lead, () => true)
      const now = (await $.clock.now()) / 1000
      const plan = newPlan(now, e.check_min, e.eta_min, Boolean(e.check_min || e.eta_min))
      await update($, plans, cur => ({ ...cur, ...Object.fromEntries(made.map(m => [m.id, plan])) }))
      void tick($)
      return text(`已派 ${made.length} 个：${made.map(m => `${m.id} ${m.title}`).join('；')}\n${planLine(plan)}`)
    } catch (err) {
      return fail(err)
    }
  })

  on('tool.call', { tool: 'mcp__crew__workflow' }, async ($, e) => {
    try {
      const action = String(e.action ?? '')
      const id = String(e.id ?? '')
      const needId = () => {
        if (!id) throw new Error(`action=${action} 要给 id`)
      }
      if (action === 'run') {
        const me = await owner($)
        const via = e.agentId ? agents.get(e.agentId) : undefined
        const spec = {
          script: e.script, script_path: e.script_path, args: e.args, ...(e.concurrency != null ? { concurrency: e.concurrency } : {}),
          cwd: e.cwd ?? (await $.session.cwd()), owner: me,
          origin: { session: await $.session.id(), cwd: await $.session.root(), ...(via ? { via } : {}) },
        }
        const r = (await crew($, ['wf', 'run', '--stdin', '--owner', me], { stdin: JSON.stringify(spec), timeoutMs: 60000 })) as { id: string; name: string; phases: string[] }
        await update($, lead, () => true)
        await update($, wfSel, () => r.id)
        const plan = newPlan((await $.clock.now()) / 1000, e.check_min, e.eta_min, Boolean(e.check_min || e.eta_min))
        await update($, plans, cur => ({ ...cur, [r.id]: plan }))
        void tick($)
        return text(`已开始 ${r.id}「${r.name}」${r.phases.length ? `（${r.phases.join(' → ')}）` : ''}，结束时叫醒你\n${planLine(plan)}`)
      }
      if (action === 'show') {
        needId()
        return text(await crew($, ['wf', 'show', id, '-v', String(Number(e.verbose ?? 0))], { json: false }))
      }
      if (action === 'result') {
        needId()
        return text(await crew($, ['wf', 'result', id, ...(e.full ? ['--full'] : [])], { json: false }))
      }
      if (action === 'list') {
        const rows = (await crew($, ['wf', 'ls', ...(e.all ? ['--all'] : ['--owner', await owner($)])])) as CrewWf[]
        return text(rows.length ? rows.map(wfLine).join('\n') : '没有工作流')
      }
      if (action === 'cancel' || action === 'resume') {
        needId()
        const out = await crew($, ['wf', action, id], { json: false })
        void tick($)
        return text(String(out).trim())
      }
      return text(`不认识的 action：${action}`)
    } catch (err) {
      return fail(err)
    }
  })

  on('tool.call', { tool: 'mcp__crew__status' }, async ($, e) => {
    try {
      const me = await owner($)
      const r = await crew($, ['ls', ...(e.all ? ['--all'] : ['--owner', me])])
      const tasks = (r.tasks as CrewTask[]).filter(t => !t.wf)
      const wfs = ((r.workflows ?? []) as CrewWf[]).filter(w => e.all || w.owner === me)
      const head = r.daemon?.alive ? `后台服务运行中（${r.daemon.host} pid ${r.daemon.pid}）` : '后台服务未运行（派任务时会自动启动）'
      const rows = tasks.map(t => `${line(t)}${t.owner === me ? '' : `  〔${t.origin}派的，不是本对话〕`}`)
      const wfRows = wfs.length ? `\n工作流（代理任务不逐个列出，用 mcp__crew__workflow action=show 看）：\n${wfs.map(wfLine).join('\n')}` : ''
      const al = ((r.alerts ?? []) as CrewAlert[]).filter(a => a.status === 'open')
      const alRows = al.length ? `\n⚠ 开着的故障告警：\n${al.map(a => `- ${alertLine(a)}`).join('\n')}` : ''
      return text(`${head}${alRows}\n${rows.length ? rows.join('\n') : '没有任务'}${wfRows}`)
    } catch (err) {
      return fail(err)
    }
  })

  on('tool.call', { tool: 'mcp__crew__inspect' }, async ($, e) => {
    try {
      const id = String(e.id)
      const part = String(e.part ?? 'result')
      const tail = Number(e.tail ?? (part === 'transcript' ? 8 : 40))
      if (part === 'log') {
        const lines = (await crew($, ['log', id, '--fmt', '-n', String(tail), '-w', '300'])) as string[]
        return text(lines.join('\n'))
      }
      if (part === 'transcript') {
        return text(await crew($, ['transcript', id, '-n', String(tail), '--chars', '1200'], { json: false }))
      }
      if (part === 'task') return text(await crew($, ['task', id], { json: false }))
      if (part === 'full') return text(await crew($, ['show', id], { json: false }))
      return text(String(await crew($, ['digest', id, '--as', await owner($)], { json: false })).trim())
    } catch (err) {
      return fail(err)
    }
  })

  on('tool.call', { tool: 'mcp__crew__review' }, async ($, e) => {
    try {
      const me = await owner($)
      const ids = [...((e.ids as string[] | undefined) ?? []), ...(e.id ? [String(e.id)] : [])]
      if (!ids.length) {
        return text('要给 id 或 ids')
      }
      const ok: string[] = []
      const bad: string[] = []
      for (const id of ids) {
        const args = ['review', id, String(e.verdict), String(e.feedback ?? ''), '--from', 'Claude', '--as', me]
        if (typeof e.score === 'number') {
          args.push('-s', String(e.score))
        }
        try {
          ok.push(((await crew($, args)) as { id: string }).id)
        } catch (err) {
          bad.push(`${id}：${String((err as Error).message ?? err).slice(0, 200)}`)
        }
      }
      void tick($)
      const verb = e.verdict === 'accept' ? '通过' : e.verdict === 'revise' ? '退回修改' : '否决'
      return text(`${ok.length ? `已${verb} ${ok.join(' ')}` : ''}${bad.length ? `${ok.length ? '\n' : ''}没成功：\n${bad.join('\n')}` : ''}`)
    } catch (err) {
      return fail(err)
    }
  })

  on('tool.call', { tool: 'mcp__crew__steer' }, async ($, e) => {
    try {
      const r = await crew($, ['say', String(e.id), String(e.message), '--from', 'Claude', '--as', await owner($)])
      return text(`已发给 ${r.id}（它当前状态：${ZH[r.status_before] ?? r.status_before}；${r.status_before === 'waiting' ? '这条作为对提问的回答，任务会继续跑' : '工人下一回合读到'}）`)
    } catch (err) {
      return fail(err)
    }
  })

  on('tool.call', { tool: 'mcp__crew__message' }, async ($, e) => {
    try {
      const me = await $.session.id()
      const peers = (await crew($, ['peers', '--me', me])) as Peer[]
      const self = peers.find(p => p.me)
      const others = peers.filter(p => !p.me)
      const to = String(e.to ?? '').trim()
      const body = String(e.text ?? '').trim()
      const show = (p: Peer) => `- 「${p.topic || '（无标题）'}」 ${p.name} ${p.session.slice(0, 8)} ${p.status === 'busy' ? '忙' : '空闲'}${p.crew_tasks + p.crew_workflows ? `；crew 近 3 天任务 ${p.crew_tasks}、工作流 ${p.crew_workflows}，在跑 ${p.crew_active}` : '；没用 crew'}`
      if (!to || !body) {
        return text(others.length ? `本机开着的其他对话：\n${others.map(show).join('\n')}` : '本机没有其他开着的对话')
      }
      let targets: Peer[]
      if (to === 'crew') {
        targets = others.filter(p => p.crew_tasks + p.crew_workflows > 0)
      } else {
        const exact = others.filter(p => p.name === to || p.session.startsWith(to))
        targets = exact.length ? exact : others.filter(p => p.topic.includes(to))
        if (targets.length > 1) {
          return fail(new Error(`「${to}」对上了 ${targets.length} 个对话，写得更具体些（名字或会话 id 前缀）：\n${targets.map(show).join('\n')}`))
        }
      }
      if (!targets.length) {
        return fail(new Error(`没找到「${to}」。本机开着的其他对话：\n${others.map(show).join('\n') || '（没有）'}`))
      }
      const from = self ? `「${self.topic || self.name}」（${self.name}）` : me.slice(0, 8)
      const msg = `[跨对话消息] 来自 ${from}：\n${body}\n\n（这是另一个 Claude 对话发来的，不是用户本人。需要回复就用 crew 的 message 工具，to 写 ${self?.name ?? me.slice(0, 8)}。）`
      const res: string[] = []
      for (const p of targets) {
        const r = await $.session.send({ to: { sessionId: p.session }, text: msg })
        res.push(`- 「${p.topic || p.name}」（${p.name}）：${r.isDelivered ? '已送达' : `没送达：${r.reason}`}`)
        await crew($, ['msgs', '--add'], {
          stdin: JSON.stringify({ from: me, from_topic: self?.topic ?? '', to: p.session, to_topic: p.topic, text: body, delivered: r.isDelivered, reason: r.isDelivered ? undefined : r.reason }),
        }).catch(() => undefined)
      }
      return text(res.join('\n'))
    } catch (err) {
      return fail(err)
    }
  })

  on('tool.call', { tool: 'mcp__crew__alert' }, async ($, e) => {
    try {
      const me = await owner($)
      const sid = await $.session.id()
      const all = (await crew($, ['alerts'])) as CrewAlert[]
      const mineA = (a: CrewAlert) => !a.owners?.length || a.owners.includes(me) || (a.sessions ?? []).includes(sid)
      const show = (a: CrewAlert) => `- ${a.key}：${a.status === 'open' ? '⚠ 开着' : a.status === 'resolved' ? '✓ 已恢复' : '安静了'} ${a.title ?? a.label}（${a.count} 次，波及 ${a.tasks.length} 个任务；来源：${a.source}）`
      const action = String(e.action)
      if (action === 'list') {
        const rows = all.filter(mineA)
        return text(rows.length ? rows.map(show).join('\n') : '没有和本对话有关的告警')
      }
      const k = String(e.key ?? '').trim()
      if (!k) return fail(new Error('要写 key（先 list 看）'))
      const hits = all.filter(a => a.key === k || a.key.startsWith(k))
      if (!hits.length) return fail(new Error(`没有 key 为 ${k} 的告警`))
      if (hits.length > 1) return fail(new Error(`${k} 对上了多条，写全：\n${hits.map(show).join('\n')}`))
      const a = hits[0]
      if (!mineA(a)) return fail(new Error(`${a.key} 是别的对话的任务遇到的告警，本对话不能动`))
      if (action === 'delete') {
        await crew($, ['alerts', '--delete', a.key])
      } else {
        if (a.status !== 'open') return text(`${a.key} 已经不是开着的了；要从列表里去掉用 delete`)
        await crew($, ['alerts', '--resolve', a.key, '--why', String(e.why ?? '') || `对话 ${me} 确认已解决`])
      }
      void tick($)
      return text(`${action === 'delete' ? '已删除' : '已标记解决'}：${a.title ?? a.label}`)
    } catch (err) {
      return fail(err)
    }
  })

  on('tool.call', { tool: 'mcp__crew__checkin' }, async ($, e) => {
    try {
      const now = (await $.clock.now()) / 1000
      const id = String(e.id ?? '')
      const cur = await read($, plans)
      if (!id) {
        const rows = Object.entries(cur).map(([k, p]) => `- ${k}：${p.eta ? `预计 ${p.eta} 分钟` : '没给预计时间'}，每 ${p.every} 分钟，下次 ${utcHM(p.next)}，已查看 ${p.checks} 次`)
        return text(rows.length ? rows.join('\n') : '本对话没有在跑的工作流或任务')
      }
      const old = cur[id] ?? newPlan(now, undefined, undefined, false)
      const eta = Number(e.eta_min) > 0 ? Math.round(Number(e.eta_min)) : old.eta
      const every = Number(e.check_min) > 0 ? planEvery(e.check_min) : old.set ? old.every : planEvery(undefined, eta)
      const plan: CrewPlan = { ...old, eta, every, next: now + every * 60, set: true }
      await update($, plans, c => ({ ...c, [id]: plan }))
      return text(`${id}：下次查看 ${utcHM(plan.next)}（${every} 分钟后）${eta ? `，预计共 ${eta} 分钟` : ''}`)
    } catch (err) {
      return fail(err)
    }
  })

  on('tool.call', { tool: 'mcp__crew__control' }, async ($, e) => {
    try {
      const id = String(e.id)
      const action = String(e.action)
      const args = action === 'retry' ? ['resume', id, '--retry'] : [action, id]
      await crew($, [...args, '--from', 'Claude', '--as', await owner($)])
      void tick($)
      return text(`已投递 ${action} → ${id}`)
    } catch (err) {
      return fail(err)
    }
  })

  on('tool.call', { tool: 'mcp__crew__wait' }, async ($, e) => {
    try {
      const timeout = Math.min(540, Math.max(5, Number(e.timeout_s ?? 300)))
      const ids = (e.ids as string[] | undefined) ?? []
      const r = await crew($, ['wait', ...ids, '--owner', await owner($), '--timeout', String(timeout)], { timeoutMs: (timeout + 20) * 1000 })
      const ready = r.ready as CrewTask[]
      const pending = r.pending as CrewTask[]
      if (ready.length) {
        await claimKeys($, ready.map(needKey))      // 已经交给你看了：不再另外叫醒
      }
      return text(`${ready.length ? `需要处理：\n${ready.map(line).join('\n')}` : '等到超时，还没有任务需要处理'}${pending.length ? `\n还在跑：\n${pending.map(line).join('\n')}` : ''}`)
    } catch (err) {
      return fail(err)
    }
  })

  // ───────── /crew
  on('command.run', { command: 'crew' }, async ($, e) => {
    const args = e.args.trim().split(/\s+/).filter(Boolean)
    if (args[0] === 'off') {
      await update($, lead, () => false)
      return { text: 'crew：本对话不再附带 crew 使用说明（工具照常可用，任务照常跑）。' }
    }
    if (args[0] === 'auto') {
      const on_ = args[1] !== 'off'
      await update($, auto, () => on_)
      return { text: `crew：自动叫醒已${on_ ? '打开' : '关闭'}。` }
    }
    if (args[0] === 'keep') {
      const on_ = args[1] !== 'off'
      await update($, keep, () => on_)
      return { text: `crew：定时查看已${on_ ? '打开（按每项的查看计划到点提醒，没定的默认 45 分钟；最多连续 20 次）' : '关闭'}。` }
    }
    if (args[0] === 'parallel') {
      return { text: String(await crew($, ['parallel', ...args.slice(1)], { json: false })) }
    }
    if (args[0] === 'help' || args[0] === 'top') {
      return { text: String(await crew($, ['help'], { json: false })) }
    }
    await update($, lead, () => true)
    await update($, scope, () => 'mine' as const)
    await update($, conv, () => '')
    await update($, mode, () => 'home' as const)
    await openPane($)
    return { text: 'crew 面板已打开（/crew help 看使用说明）' }
  })

  // ───────── 面板
  // ───────── 输入框上方的进度栏：像 Claude 自己的工作流 / 子代理列表那样，一个工作流一棵树（阶段 → 代理），任务逐个列出
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) {
      return next(e)
    }
    const s = await read($, snap)
    const me = await owner($)
    const mySid = await $.session.id()
    const trees = await read($, bandWfs)
    const mine = (t: CrewTask) => t.owner === me || t.session === mySid
    const tasks = s.tasks.filter(t => !t.wf && mine(t) && (ACTIVE.includes(t.status) || NEEDS.includes(t.status)))
    const alerts = (s.alerts ?? []).filter(a => a.status === 'open' && a.important && (a.owners.includes(me) || a.sessions.includes(mySid)))
    if (!trees.length && !tasks.length && !alerts.length) {
      return next(e)
    }
    const { Box, Text } = $.ui.resolve(e)
    const W = Math.max(40, e.props.bodyColumns)
    const frame = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'[Math.floor(Date.now() / 1000) % 10]
    const clip = (x: string, n: number) => (x.length > n ? `${x.slice(0, Math.max(1, n - 1))}…` : x)
    const bar = (done: number, total: number, n = 10) => {
      const k = total ? Math.round((done / total) * n) : 0
      return '━'.repeat(k) + '╌'.repeat(n - k)
    }
    const AG: Record<string, [string, string]> = {
      running: [frame, 'claude'], queued: ['○', 'inactive'], retry: ['↻', 'warning'], paused: ['‖', 'warning'],
      done: ['✓', 'success'], failed: ['✗', 'error'], cancelled: ['·', 'inactive'],
    }
    const TS: Record<string, [string, string, string]> = {
      running: [frame, 'claude', '运行'], queued: ['○', 'inactive', '排队'], paused: ['‖', 'warning', '暂停'],
      waiting: ['?', 'permission', '提问'], review: ['◆', 'success', '待验收'], failed: ['✗', 'error', '失败'],
    }
    const rows: unknown[] = []
    const nRun = trees.filter(t => !WF_DONE.includes(t.wf.status)).length
    const head = [nRun && `${nRun} 个工作流在跑`, tasks.length && `${tasks.length} 个任务`].filter(Boolean).join(' · ')
    rows.push(
      <Text key="h" wrap="truncate">
        <Text bold color="claude">crew</Text>
        <Text dimColor>{`  ${head}${s.daemonUp ? '' : ' · 后台未运行'}  ·  /crew 打开面板`}</Text>
      </Text>,
    )
    for (const a of alerts.slice(0, 2)) {
      rows.push(<Text key={`al-${a.key}`} color="error" wrap="truncate">{`⚠ 故障：${a.title}（${a.count} 次，波及 ${a.tasks.length} 个任务）`}</Text>)
    }
    trees.forEach((tr, wi) => {
      const w = tr.wf
      const c = w.counts ?? {}
      const done = (c.done ?? 0) + (c.failed ?? 0) + (c.cancelled ?? 0)
      const lastWf = wi === trees.length - 1 && !tasks.length
      const [g, col] = w.status === 'running' ? [frame, 'claude'] : w.status === 'done' ? ['✓', 'success'] : w.status === 'failed' ? ['✗', 'error'] : w.status === 'cancelled' ? ['·', 'inactive'] : ['○', 'inactive']
      rows.push(
        <Text key={`w-${w.id}`} wrap="truncate">
          <Text dimColor>{lastWf ? '└─ ' : '├─ '}</Text>
          <Text color={col}>{`${g} `}</Text>
          <Text bold>{w.name}</Text>
          <Text dimColor>{`  工作流 · ${WF_ZH[w.status] ?? w.status}  `}</Text>
          <Text color={col}>{bar(done, w.agents)}</Text>
          <Text dimColor>{`  代理 ${done}/${w.agents} · ${fmtSecs(w.elapsed)} · ${fmtTok(w.tokens)} tok`}</Text>
        </Text>,
      )
      const pre = lastWf ? '   ' : '│  '
      const phases = tr.phases.filter(p => p.total || p.status !== 'pending')
      const pend = tr.phases.filter(p => !p.total && p.status === 'pending')
      phases.forEach((p, pi) => {
        const lastP = pi === phases.length - 1 && !pend.length
        const pg = p.status === 'done' ? (p.failed ? '✗' : '✓') : p.status === 'running' ? frame : '○'
        const pc = p.status === 'done' ? (p.failed ? 'error' : 'success') : p.status === 'running' ? 'claude' : 'inactive'
        rows.push(
          <Text key={`p-${w.id}-${pi}`} wrap="truncate">
            <Text dimColor>{pre + (lastP ? '└─ ' : '├─ ')}</Text>
            <Text color={pc}>{`${pg} `}</Text>
            <Text bold={p.status === 'running'}>{p.title}</Text>
            <Text dimColor>{`  ${p.done}/${p.total}${p.failed ? ` · 失败 ${p.failed}` : ''}${p.total ? ` · ${fmtSecs(p.elapsed)}` : ''}${p.detail && p.status === 'running' ? ` · ${p.detail}` : ''}`}</Text>
          </Text>,
        )
        // 只展开在跑的阶段（和整个工作流只有一个阶段时）：代理逐个列出
        if (p.status !== 'running' && phases.length > 1) {
          return
        }
        const pre2 = pre + (lastP ? '   ' : '│  ')
        const order = (a: CrewWfAgent) => (a.status === 'running' ? 0 : a.status === 'retry' ? 1 : a.status === 'queued' ? 3 : 2)
        const ags = [...p.agents].sort((x, y) => order(x) - order(y))
        const shown = ags.slice(0, 6)
        shown.forEach((a, ai) => {
          const lastA = ai === shown.length - 1 && ags.length <= 6
          const [ag, ac] = AG[a.status] ?? ['·', 'inactive']
          const tail = ['running', 'retry'].includes(a.status)
            ? (a.step ?? '')
            : a.status === 'done' ? (a.summary ?? '') : a.status === 'failed' ? (a.note ?? '') : ''
          const meta = a.status === 'queued' ? '排队' : `${a.turns ?? 0} 回合 · ${fmtSecs(a.elapsed)} · ${fmtTok(a.tokens ?? 0)}`
          const left = `${pre2}${lastA ? '└─ ' : '├─ '}${ag} ${a.label}  `
          rows.push(
            <Text key={`a-${w.id}-${pi}-${a.n}`} wrap="truncate">
              <Text dimColor>{pre2 + (lastA ? '└─ ' : '├─ ')}</Text>
              <Text color={ac}>{`${ag} `}</Text>
              <Text>{a.label}</Text>
              <Text dimColor>{`  ${meta}`}</Text>
              {tail ? <Text dimColor>{`  ${clip(tail.replace(/\s+/g, ' '), Math.max(10, W - left.length * 1.6 - meta.length - 6))}`}</Text> : null}
            </Text>,
          )
        })
        if (ags.length > 6) {
          rows.push(<Text key={`am-${w.id}-${pi}`} dimColor wrap="truncate">{`${pre2}└─ 还有 ${ags.length - 6} 个代理`}</Text>)
        }
      })
      if (pend.length) {
        rows.push(
          <Text key={`pp-${w.id}`} dimColor wrap="truncate">{`${pre}└─ ○ 后面还有：${pend.map(p => p.title).join(' → ')}`}</Text>,
        )
      }
    })
    const order = (t: CrewTask) => ({ waiting: 0, review: 1, failed: 2, running: 3, paused: 4, queued: 5 } as Record<string, number>)[t.status] ?? 9
    const ts = [...tasks].sort((x, y) => order(x) - order(y))
    const shownT = ts.slice(0, 8)
    shownT.forEach((t, i) => {
      const last = i === shownT.length - 1 && ts.length <= 8
      const [g, col, zh] = TS[t.status] ?? ['·', 'inactive', ZH[t.status] ?? t.status]
      const meta = t.status === 'queued' ? '' : `${t.turns} 回合 · ${fmtSecs(t.elapsed)} · ${fmtTok(t.tokens)}`
      const tail = t.status === 'waiting' ? (t.question ?? '') : t.status === 'running' ? (t.step ?? '') : t.status === 'failed' ? (t.note ?? '') : ''
      rows.push(
        <Text key={`t-${t.id}`} wrap="truncate">
          <Text dimColor>{last ? '└─ ' : '├─ '}</Text>
          <Text color={col}>{`${g} `}</Text>
          <Text>{t.title}</Text>
          <Text color={col}>{`  ${zh}`}</Text>
          <Text dimColor>{meta ? `  ${meta}` : ''}</Text>
          {tail ? <Text dimColor>{`  ${clip(tail.replace(/\s+/g, ' '), Math.max(10, W - t.title.length * 2 - 30))}`}</Text> : null}
        </Text>,
      )
    })
    if (ts.length > 8) {
      rows.push(<Text key="tm" dimColor wrap="truncate">{`└─ 还有 ${ts.length - 8} 个任务（/crew 看全部）`}</Text>)
    }
    return <Box flexDirection="column">{rows}</Box>
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Input } = $.ui.resolve(e)
    const s = await read($, snap)
    const { list: shown, mine } = await visibleTasks($, s.tasks)
    const all = (await read($, scope)) === 'all'
    const picked = await read($, selected)
    const sel = shown.some(t => t.id === picked) ? picked : shown[0]?.id ?? ''
    const d = await read($, detail)
    const b = await read($, brief)
    const c = await read($, chat)
    const view = await read($, tab)
    const rmId = await read($, confirmRm)
    const isAuto = await read($, auto)
    const cols = Math.max(30, e.props.bodyColumns)
    const counts = ['running', 'queued', 'waiting', 'review', 'failed']
      .map(k => [k, shown.filter(t => t.status === k).length] as const)
      .filter(([, n]) => n)
      .map(([k, n]) => `${ZH[k]} ${n}`)
    const cur = shown.find(t => t.id === sel)
    const order = ['waiting', 'review', 'failed', 'running', 'paused', 'queued']
    const tasks = [...shown].sort((a, b) => {
      const ia = order.indexOf(a.status)
      const ib = order.indexOf(b.status)
      return (ia < 0 ? 9 : ia) - (ib < 0 ? 9 : ib)
    })
    const room = Math.max(3, Math.min(tasks.length, Math.floor(((e.viewport?.rows ?? 30) - 12) / 2)))
    const m = await read($, mode)
    const cv = await read($, conv)
    const wfs = s.workflows ?? []
    const wfActive = wfs.filter(w => !WF_DONE.includes(w.status))
    const go = (to: 'home' | 'tasks' | 'wf', extra?: () => Promise<unknown>) => void update($, mode, () => to)
      .then(() => update($, confirmRm, () => ''))
      .then(() => (extra ? extra() : undefined))
      .then(() => tick($))
    const allTasks = s.tasks.filter(t => !t.wf)
    const nav = (
      <Box gap={1}>
        <Button key="nav-home" label={m === 'home' ? '[主页]' : '主页'} onPress={() => go('home')} />
        <Button key="nav-wf" label={`${m === 'wf' ? '[' : ''}工作流 ${wfActive.length}${m === 'wf' ? ']' : ''}`} onPress={() => go('wf')} />
        <Button key="nav-tasks" label={`${m === 'tasks' ? '[' : ''}任务 ${allTasks.filter(t => ACTIVE.includes(t.status) || NEEDS.includes(t.status)).length}${m === 'tasks' ? ']' : ''}`} onPress={() => go('tasks', () => update($, conv, () => ''))} />
      </Box>
    )
    const svc = (
      <Text wrap="truncate">
        <Text color={s.daemonUp ? 'success' : 'error'}>{s.daemonUp ? '● 后台运行中' : '○ 后台未运行'}</Text>
        <Text dimColor>{s.daemonUp ? '，关掉 Claude 也不停' : '  派任务或运行工作流时自动启动'}{'  自动叫醒 '}{isAuto ? '开' : '关'}</Text>
      </Text>
    )
    const myS = await $.session.id()
    const meO = await owner($)

    if (m === 'home') {
      const wfSorted = [...wfs].sort((a, b) => Number(WF_DONE.includes(a.status)) - Number(WF_DONE.includes(b.status)) || a.elapsed - b.elapsed)
      const wfCount = (st: string[]) => wfs.filter(w => st.includes(w.status)).length
      const convs = new Map<string, { name: string; tasks: CrewTask[]; mine: boolean }>()
      for (const t of allTasks) {
        const k = convKey(t)
        const g = convs.get(k) ?? { name: convName(t), tasks: [], mine: t.session === myS || t.owner === meO }
        g.tasks.push(t)
        convs.set(k, g)
      }
      const live = (g: { tasks: CrewTask[] }) => g.tasks.filter(t => ACTIVE.includes(t.status) || NEEDS.includes(t.status)).length
      const convList = [...convs.entries()].sort((a, b) => Number(b[1].mine) - Number(a[1].mine) || live(b[1]) - live(a[1])).filter(([, g]) => live(g)).slice(0, 5)
      const needs = allTasks.filter(t => NEEDS.includes(t.status)).slice(0, 5)
      const tc = (st: string) => allTasks.filter(t => t.status === st).length
      return (
        <Box flexDirection="column">
          {svc}
          {nav}
          {s.error ? <Text color="error" wrap="truncate">{s.error}</Text> : null}
          {(s.alerts ?? []).filter(a => a.status !== 'quiet').slice(0, 4).map(a => (
            <Text key={`al-${a.key}`} color={a.status === 'open' ? 'error' : undefined} dimColor={a.status !== 'open'} wrap="truncate">
              {a.status === 'open' ? `⚠ 故障：${alertLine(a)}` : `✓ 已恢复 ${hhmm(a.resolved)}：${a.title}`}
            </Text>
          ))}
          <Box flexDirection="column" marginTop={1}>
            <Text wrap="truncate">
              <Text bold color="claude">工作流</Text>
              <Text dimColor>{'  '}运行 {wfCount(['running', 'starting'])} · 完成 {wfCount(['done'])} · 失败 {wfCount(['failed'])} · 取消 {wfCount(['cancelled'])}（24 小时内）</Text>
            </Text>
            {!wfs.length ? <Text dimColor>  还没有工作流：让 Claude 写好脚本后用 mcp__crew__workflow action=run 交给 crew</Text> : null}
            {wfSorted.slice(0, 5).map(w => (
              <Button key={`hw-${w.id}`} plain onPress={() => go('wf', () => update($, wfSel, () => w.id).then(() => update($, agentSel, () => '')))}>
                <Text color={w.status === 'running' ? 'success' : w.status === 'failed' ? 'error' : 'subtle'}>  {w.status === 'running' ? '▶' : w.status === 'done' ? '✓' : w.status === 'failed' ? '✗' : '·'} </Text>
                <Text bold>{w.name.slice(0, Math.max(10, cols - 46))}</Text>
                <Text dimColor> {WF_ZH[w.status] ?? w.status} {fmtSecs(w.elapsed)} 代理 {w.counts?.done ?? 0}/{w.agents}{w.counts?.failed ? ` ✗${w.counts.failed}` : ''} {fmtTok(w.tokens)}{(w.owner === meO || w.session === myS) ? ' · 本对话' : ` · ${clipTo(w.origin, 20)}`}</Text>
              </Button>
            ))}
            {wfs.length > 5 ? <Text dimColor>  …另 {wfs.length - 5} 个，点「工作流」看全部</Text> : null}
          </Box>
          <Box flexDirection="column" marginTop={1}>
            <Text wrap="truncate">
              <Text bold color="suggestion">任务</Text>
              <Text dimColor>{'  '}运行 {tc('running')} · 排队 {tc('queued')} · 待验收 {tc('review')} · 提问 {tc('waiting')} · 失败 {tc('failed')}</Text>
            </Text>
            {convList.length ? <Text dimColor>  按对话（点进去只看那个对话的）：</Text> : <Text dimColor>  没有进行中的代理</Text>}
            {convList.map(([k, g]) => {
              const c2 = (st: string) => g.tasks.filter(t => t.status === st).length
              const bits = [['running', '运行'], ['queued', '排队'], ['review', '待验收'], ['waiting', '提问'], ['failed', '失败']]
                .filter(([st]) => c2(st)).map(([st, zh]) => `${zh} ${c2(st)}`).join(' · ')
              return (
                <Button key={`hc-${k}`} plain onPress={() => go('tasks', () => update($, conv, () => k).then(() => update($, selected, () => '')))}>
                  <Text>    {clipTo(g.name, Math.max(16, cols - 40))}</Text>
                  <Text dimColor> {bits}{g.mine ? ' · 本对话' : ''}</Text>
                </Button>
              )
            })}
            {needs.length ? <Text dimColor>  等人处理：</Text> : null}
            {needs.map(t => (
              <Button key={`hn-${t.id}`} plain onPress={() => go('tasks', () => update($, conv, () => convKey(t)).then(() => update($, selected, () => t.id)).then(() => update($, tab, () => 'chat' as const)))}>
                <Text color={COLOR[t.status]}>    {GLYPH[t.status]} {ZH[t.status]}</Text>
                <Text> {t.title.slice(0, Math.max(8, cols - 40))}</Text>
                <Text dimColor> {fmtSecs(t.elapsed)} · {clipTo(convName(t), 18)}</Text>
              </Button>
            ))}
          </Box>
          <Text dimColor wrap="wrap">{'\n'}终端里同样的主页：crew top（Tab 切换 主页 / 工作流 / 任务）；单个工作流实时进度：crew wf watch &lt;id&gt;</Text>
        </Box>
      )
    }

    if (m === 'wf') {
      const { list: wl, mine: wMine } = await visibleWfs($, wfs)
      const ws = await read($, wfSel)
      const wid = wl.some(w => w.id === ws) ? ws : wl[0]?.id ?? ''
      const tr = await read($, wfTree)
      const aSel = await read($, agentSel)
      const al = await read($, agentLog)
      const cur = tr && tr.wf.id === wid ? tr : null
      const wcur = wl.find(w => w.id === wid)
      let budgetRows = Math.max(8, (e.viewport?.rows ?? 30) - 16 - Math.min(wl.length, 4))
      return (
        <Box flexDirection="column">
          {svc}
          {nav}
          {s.error ? <Text color="error" wrap="truncate">{s.error}</Text> : null}
          <Box gap={1}>
            <Button key="wf-scope" label={all ? '只看本对话' : '看全部对话'} onPress={() => void update($, scope, v => (v === 'all' ? 'mine' : 'all')).then(() => tick($))} />
          </Box>
          {!wl.length ? <Text dimColor>{all ? '没有工作流' : '本对话还没有托管工作流；点上面的按钮看全部'}</Text> : null}
          {wl.slice(0, 4).map(w => (
            <Button key={`w-${w.id}`} plain onPress={() => void update($, wfSel, () => w.id).then(() => update($, agentSel, () => '')).then(() => tick($))}>
              <Text color={w.status === 'running' ? 'success' : w.status === 'failed' ? 'error' : 'subtle'}>{w.status === 'running' ? '▶' : w.status === 'done' ? '✓' : w.status === 'failed' ? '✗' : '·'} </Text>
              <Text bold={w.id === wid}>{w.name}</Text>
              <Text dimColor> {WF_ZH[w.status] ?? w.status} {fmtSecs(w.elapsed)} {w.counts?.done ?? 0}/{w.agents}{wMine(w) ? '' : ` · ${clipTo(w.origin, 20)}`}</Text>
            </Button>
          ))}
          {wl.length > 4 ? <Text dimColor>…另 {wl.length - 4} 个（终端 crew wf ls）</Text> : null}
          {wcur && cur ? (
            <Box flexDirection="column" marginTop={1}>
              <Text bold wrap="truncate">{cur.wf.id} · {cur.wf.name} · {WF_ZH[cur.wf.status] ?? cur.wf.status} · {fmtSecs(cur.wf.elapsed)} · 代理 {cur.wf.agents} · {fmtTok(cur.wf.tokens)} tok{cur.attempt > 1 ? ` · 第 ${cur.attempt} 次启动` : ''}</Text>
              {cur.description ? <Text dimColor wrap="truncate">{cur.description}</Text> : null}
              <Box gap={1}>
                {!WF_DONE.includes(cur.wf.status)
                  ? <Button key="wf-cancel" label="取消工作流" onPress={() => void act($, crew($, ['wf', 'cancel', cur.wf.id], { json: false }), `已取消 ${cur.wf.id}`)} />
                  : <Button key="wf-resume" label="续跑" onPress={() => void act($, crew($, ['wf', 'resume', cur.wf.id], { json: false }))} />}
                {WF_DONE.includes(cur.wf.status) && wMine(wcur)
                  ? <Button key="wf-hand" variant="primary" label="把结果交给本对话" onPress={() => void act($, crew($, ['wf', 'result', cur.wf.id], { json: false }).then(r => $.prompt.submit({ text: `[crew] 工作流结果：\n${String(r).trim()}` })))} />
                  : null}
              </Box>
              {cur.phases.map(p => {
                const done = p.agents.filter(a => ['done', 'failed', 'cancelled'].includes(a.status))
                const open = p.agents.filter(a => !['done', 'failed', 'cancelled'].includes(a.status))
                const failed = p.agents.filter(a => a.status === 'failed')
                const showN = Math.max(2, Math.min(10, budgetRows))
                const pick = p.agents.length <= showN ? p.agents : [...failed, ...open, ...done.filter(a => a.status !== 'failed').slice(-2)].slice(0, showN)
                budgetRows -= pick.length + 1
                return (
                  <Box key={`p-${p.title}`} flexDirection="column">
                    <Text wrap="truncate">
                      <Text color={p.status === 'running' ? 'success' : p.status === 'done' ? 'subtle' : undefined}>{p.status === 'done' ? '✓' : p.status === 'running' ? '▶' : '·'} </Text>
                      <Text bold>{p.title}</Text>
                      <Text dimColor>  {p.done}/{p.total}{p.failed ? ` 失败 ${p.failed}` : ''}{p.total ? `  ${fmtSecs(p.elapsed)}` : ''}{p.detail ? `  — ${p.detail}` : ''}</Text>
                    </Text>
                    {pick.map(a => (
                      <Button key={`a-${a.n}`} plain onPress={() => void update($, agentSel, () => (a.task && aSel !== a.task ? a.task : '')).then(() => tick($))}>
                        <Text color={A_COLOR[a.status]}>   {A_GLYPH[a.status] ?? ' '} </Text>
                        <Text bold={Boolean(a.task) && a.task === aSel}>{a.label.slice(0, 28)}</Text>
                        <Text dimColor> {A_ZH[a.status] ?? a.status} {fmtSecs(a.elapsed)} {a.turns}回合 {fmtTok(a.tokens)}{a.model ? ` ${a.model}` : ''}{a.cached ? ' 缓存' : ''}{a.cont ? ' 续上文' : ''}{a.retries ? ` 重试${a.retries}` : ''} </Text>
                        <Text dimColor>{(['running', 'queued', 'retry', 'paused'].includes(a.status)
                          ? `${a.step ?? ''}${a.step_ago != null ? `（${fmtSecs(a.step_ago)}前）` : ''}`
                          : a.status === 'failed' ? `✗ ${a.note}` : a.status === 'cancelled' ? '已取消' : `→ ${a.summary}`).slice(0, Math.max(10, cols - 50))}</Text>
                      </Button>
                    ))}
                    {pick.length < p.agents.length ? <Text dimColor>     …另 {p.agents.length - pick.length} 个已完成（终端 crew wf show {cur.wf.id} 看全部）</Text> : null}
                  </Box>
                )
              })}
              {cur.logs.length ? <Text dimColor>日志：</Text> : null}
              {cur.logs.slice(-4).map((l, i) => <Text key={`wl-${i}`} dimColor wrap="truncate">  {clock(l.t)} {l.msg}</Text>)}
              {cur.error ? <Text color="error" wrap="truncate">错误：{cur.error.split('\n')[0]}</Text> : null}
              {cur.wf.status === 'done' && cur.result_preview ? <Text dimColor wrap="truncate">返回值：{cur.result_preview}</Text> : null}
              {aSel && al.id === aSel ? (
                <Box flexDirection="column" marginTop={1}>
                  <Box gap={1}>
                    <Text bold color="suggestion">代理 {aSel} 最近几步</Text>
                    <Button key="a-chat" label="看完整对话" onPress={() => go('tasks', () => update($, conv, () => '').then(() => update($, scope, () => 'all' as const)).then(() => update($, selected, () => aSel)).then(() => update($, tab, () => 'chat' as const)))} />
                  </Box>
                  {al.lines.slice(-8).map((l, i) => <Text key={`al-${i}`} dimColor={!/📦|❓|⚖|●/.test(l)} wrap="truncate">{l}</Text>)}
                </Box>
              ) : <Text dimColor>点一个代理看它最近几步</Text>}
            </Box>
          ) : wid ? <Text dimColor>读取中…</Text> : null}
        </Box>
      )
    }

    return (
      <Box flexDirection="column">
        <Text wrap="truncate">
          <Text color={s.daemonUp ? 'success' : 'error'}>{s.daemonUp ? '● 服务运行' : '○ 服务未运行'}</Text>
          <Text dimColor>{'  '}{all ? '全部对话' : '本对话'}{'：'}{counts.join(' · ') || '没有进行中的任务'}{'  自动叫醒 '}{isAuto ? '开' : '关'}</Text>
        </Text>
        {nav}
        {cv ? (
          <Box gap={1}>
            <Text color="suggestion">只看「{clipTo(shown[0] ? convName(shown[0]) : cv.slice(0, 8), 30)}」派的</Text>
            <Button key="conv-clear" label="看本对话的" onPress={() => void update($, conv, () => '').then(() => update($, scope, () => 'mine' as const)).then(() => tick($))} />
          </Box>
        ) : null}
        <Box gap={1}>
          <Button key="scope" label={all ? '只看本对话' : `看全部对话（${s.tasks.length}）`} onPress={() => void update($, scope, v => (v === 'all' ? 'mine' : 'all')).then(() => update($, conv, () => '')).then(() => update($, confirmRm, () => '')).then(() => tick($))} />
        </Box>
        {!tasks.length ? <Text dimColor>{all ? '没有任务' : '本对话还没有派任务；点上面的按钮看全部对话的'}</Text> : null}
        {s.error ? <Text color="error" wrap="truncate">{s.error}</Text> : null}
        {tasks.slice(0, room).map(t => (
          <Button key={`t-${t.id}`} plain onPress={() => {
            // 点任务 = 看它和派活方的来回对话
            void update($, selected, () => t.id).then(() => update($, tab, () => 'chat' as const)).then(() => update($, confirmRm, () => '')).then(() => tick($))
          }}>
            <Text color={COLOR[t.status]}>{GLYPH[t.status] ?? ' '} {(ZH[t.status] ?? t.status).padEnd(3, '　')}</Text>
            <Text bold={t.id === sel}> {t.title.slice(0, Math.max(8, cols - 34))}</Text>
            <Text dimColor> r{t.round} {t.turns}回合 {fmtTok(t.tokens)} {fmtSecs(t.elapsed)}{mine(t) ? '' : ` · ${t.origin}`}</Text>
          </Button>
        ))}
        {tasks.length > room ? <Text dimColor>…另有 {tasks.length - room} 个（终端里 crew top 看全部）</Text> : null}
        {cur ? (
          <Box flexDirection="column" marginTop={1}>
            <Text bold wrap="truncate">{cur.id} · {cur.title} · {ZH[cur.status]}</Text>
            <Text dimColor wrap="truncate">派发：{cur.origin}{mine(cur) ? '（本对话）' : ''}</Text>
            {cur.question ? <Text color="permission" wrap="wrap">❓ {cur.question}</Text> : null}
            <Box gap={1}>
              {cur.status === 'paused'
                ? <Button key="resume" label="继续" onPress={() => void act($, crew($, ['resume', cur.id, '--from', '你']))} />
                : ACTIVE.includes(cur.status)
                  ? <Button key="pause" label="暂停" onPress={() => void act($, crew($, ['pause', cur.id, '--from', '你']))} />
                  : null}
              {ACTIVE.includes(cur.status)
                ? <Button key="cancel" label="取消" onPress={() => void act($, crew($, ['cancel', cur.id, '--from', '你']))} />
                : null}
              {cur.status === 'review'
                ? <Button key="accept" label="通过" onPress={() => void act($, crew($, ['review', cur.id, 'accept', '人工通过', '--from', '你']))} />
                : null}
              {NEEDS.includes(cur.status)
                ? <Button key="ask" variant="primary" label={mine(cur) ? '交给本对话处理' : `交给${clipTo(cur.origin.split(' › ')[0], 24)}处理`} onPress={() => void act($, handOff($, cur))} />
                : null}
              {cur.status === 'failed'
                ? rmId === cur.id
                  ? <Button key="rm" label="确认删除" onPress={() => void crew($, ['rm', cur.id], { json: false })
                      .then(() => update($, confirmRm, () => ''))
                      .then(() => update($, selected, () => ''))
                      .then(() => $.ui.toast(`已删除 ${cur.id}`))
                      .catch(err => $.ui.toast(`删除失败：${String((err as Error).message ?? err)}`))
                      .then(() => tick($))} />
                  : <Button key="rm" label="删除" onPress={() => void update($, confirmRm, () => cur.id)} />
                : null}
              <Button key="auto" label={isAuto ? '关自动叫醒' : '开自动叫醒'} onPress={() => void update($, auto, v => !v)} />
            </Box>
            <Input key="say" placeholder={cur.status === 'waiting' ? '回答工人的提问，回车发送' : '插话给工人，回车发送'} onSubmit={v => {
              if (v.trim()) {
                void act($, crew($, ['say', cur.id, v.trim(), '--from', '你']))
              }
            }} />
            <Box gap={1} marginTop={1}>
              <Button key="tab-chat" label={view === 'chat' ? '[对话]' : '对话'} onPress={() => void update($, tab, () => 'chat' as const).then(() => tick($))} />
              <Button key="tab-brief" label={view === 'brief' ? '[任务说明]' : '任务说明'} onPress={() => void update($, tab, () => 'brief' as const).then(() => tick($))} />
              <Button key="tab-log" label={view === 'log' ? '[时间线]' : '时间线'} onPress={() => void update($, tab, () => 'log' as const).then(() => tick($))} />
            </Box>
            {view === 'chat'
              ? c.id === cur.id
                ? (
                    <Box flexDirection="column">
                      {c.items.map((it, i) => it.tools
                        ? <Text key={`c-${i}`} dimColor wrap="wrap">{'  '}{it.text}</Text>
                        : (
                            <Box key={`c-${i}`} flexDirection="column" marginTop={1}>
                              <Text wrap="truncate">
                                <Text bold color={WHO_COLOR[it.who]}>{it.label}：</Text>
                                <Text dimColor>{it.head ? `${it.head}  ` : ''}{clock(it.t)}</Text>
                              </Text>
                              {it.text.split('\n').map((l, j) => (
                                <Text key={`c-${i}-${j}`} wrap="wrap"><Text color={WHO_COLOR[it.who]}>▎ </Text>{l || ' '}</Text>
                              ))}
                            </Box>
                          ))}
                    </Box>
                  )
                : <Text dimColor>读取中…</Text>
              : view === 'brief'
              ? b.id === cur.id
                ? (
                    <Box flexDirection="column">
                      {b.origin_text.split('\n').map((l, i) => <Text key={`o-${i}`} dimColor wrap="wrap">{l}</Text>)}
                      <Text dimColor wrap="wrap">档案 {b.profile} · 工作目录 {b.cwd}</Text>
                      <Text bold color="suggestion">── 工人收到的任务原文</Text>
                      {b.opening.split('\n').map((l, i) => <Text key={`p-${i}`} wrap="wrap">{l || ' '}</Text>)}
                      {b.later.length ? <Text bold color="suggestion">── 之后的追加指示（插话 / 回答 / 验收）</Text> : null}
                      {b.later.map((l, i) => <Text key={`a-${i}`} wrap="wrap">{l}</Text>)}
                    </Box>
                  )
                : <Text dimColor>读取中…</Text>
              : (d.id === cur.id ? d.lines : []).slice(-10).map((l, i) => (
                  <Text key={`l-${i}`} dimColor={!/📦|❓|⚖|●/.test(l)} wrap="truncate">{l}</Text>
                ))}
          </Box>
        ) : null}
      </Box>
    )
  })

  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) {
      await update($, paneOpen, () => false)
    }
    return next(e)
  }).catch(($, e, next) => next(e))
}
