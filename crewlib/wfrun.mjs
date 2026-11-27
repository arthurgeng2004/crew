// crew 工作流运行器：跑 Claude 写的工作流脚本，脚本里的每个 agent() 变成一个工人任务（默认 GPT，跑在 Codex 里）。
//
// 脚本写法和 Claude Code 的 Workflow 一样：
//   export const meta = { name, description, phases: [{ title, detail }] }
//   phase('采集'); const r = await agent('……', { label, phase, schema, profile, cwd, files, accept, deliverable, nudge_after })
//   parallel([() => agent(…), …])  pipeline(items, stage1, stage2)  log('…')  args  budget  workflow(name|{scriptPath}, args)
//
// 用法：
//   node wfrun.mjs check <script.js>     只解析，打印 {ok, meta | error}
//   node wfrun.mjs run <运行目录>          跑（由 crew 后台服务拉起；中断后再拉起会按 journal 续跑）
//
// 运行目录（~/.crew/workflows/<id>/）：
//   run.json       运行规格（crew wf run 写）    script.js   脚本副本
//   state.json     进度快照（本进程写，面板和 crew wf show 读）
//   journal.jsonl  每个 agent() 的开始 / 结果：续跑时同样的调用直接复用结果，没跑完的任务接着等
//   result.json    脚本的返回值        ctl/cancel  取消信号
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import crypto from 'node:crypto'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const CREW_BIN = path.join(path.dirname(HERE), 'bin', 'crew')
const HOME = path.resolve(process.env.CREW_HOME || path.join(os.homedir(), '.crew'))
const TASKS = path.join(HOME, 'tasks')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const FINAL_OK = ['accepted', 'review']
const FINAL_BAD = ['cancelled', 'rejected']
const AUTO_RETRY = 2 // 工人失败（接口错误等）时自动从断点续跑的次数，之后这个 agent() 返回 null

const now = () => Date.now() / 1000

// ───────── 解析脚本
function matchBrace(src, i) {
  let depth = 0
  for (; i < src.length; i++) {
    const c = src[i]
    if (c === '"' || c === "'" || c === '`') {
      const q = c
      for (i++; i < src.length && src[i] !== q; i++) if (src[i] === '\\') i++
    } else if (c === '/' && src[i + 1] === '/') {
      while (i < src.length && src[i] !== '\n') i++
    } else if (c === '/' && src[i + 1] === '*') {
      i = src.indexOf('*/', i + 2) + 1
      if (i <= 0) return -1
    } else if (c === '{') depth++
    else if (c === '}' && --depth === 0) return i
  }
  return -1
}

export function parseScript(src) {
  const m = /export\s+const\s+meta\s*=\s*/.exec(src)
  if (!m) throw new Error('脚本要以 export const meta = { name, description, phases } 开头')
  const start = m.index + m[0].length
  if (src[start] !== '{') throw new Error('meta 必须是对象字面量')
  const end = matchBrace(src, start)
  if (end < 0) throw new Error('meta 的大括号没有闭合')
  let meta
  try {
    meta = Function(`"use strict"; return (${src.slice(start, end + 1)})`)()
  } catch (e) {
    throw new Error(`meta 不是纯字面量：${e.message}`)
  }
  if (!meta || !meta.name || !meta.description) throw new Error('meta 要有 name 和 description')
  meta.phases = Array.isArray(meta.phases) ? meta.phases.filter(p => p && p.title) : []
  const body = (src.slice(0, m.index) + src.slice(end + 1).replace(/^\s*;/, ''))
    .replace(/^(\s*)export\s+(?=(const|let|var|function|async|class)\b)/gm, '$1')
  let fn
  try {
    fn = new AsyncFunction('agent', 'parallel', 'pipeline', 'phase', 'log', 'args', 'budget', 'workflow', 'meta', 'bash', body)
  } catch (e) {
    // import 语句也在这里报错（“Cannot use import statement outside a module”）
    throw new Error(`脚本语法错误：${e.message}（脚本是纯 JS，不能 import；只能用 agent / parallel / pipeline / phase / log / args / budget / workflow / bash）`)
  }
  return { meta, fn }
}

// ───────── 小工具
function readJson(p, dflt = null) {
  try {
    return JSON.parse(fs.readFileSync(p, 'utf8'))
  } catch {
    return dflt
  }
}

function writeJson(p, obj) {
  fs.mkdirSync(path.dirname(p), { recursive: true })
  const tmp = `${p}.tmp.${process.pid}`
  fs.writeFileSync(tmp, JSON.stringify(obj, null, 1))
  fs.renameSync(tmp, p)
}

function clip(s, n) {
  s = String(s ?? '').trim()
  return s.length > n ? `${s.slice(0, n)}…` : s
}

function drop(tid, kind, payload = {}) {
  const d = path.join(TASKS, tid, 'inbox')
  fs.mkdirSync(d, { recursive: true })
  const name = `${now().toFixed(6)}-${kind}-${String(Math.floor(Math.random() * 1e5)).padStart(5, '0')}.json`
  writeJson(path.join(d, name), { kind, t: now(), from: '工作流', ...payload })
}

function sh(argv, stdin) {
  return new Promise((resolve, reject) => {
    const p = spawn(argv[0], argv.slice(1), { stdio: ['pipe', 'pipe', 'pipe'] })
    let out = ''
    let err = ''
    p.stdout.on('data', d => (out += d))
    p.stderr.on('data', d => (err += d))
    p.on('error', reject)
    p.on('close', code => (code === 0 ? resolve(out) : reject(new Error((err || out || `退出码 ${code}`).trim().slice(-600)))))
    p.stdin.end(stdin ?? '')
  })
}

// ───────── check 模式
if (process.argv[2] === 'check') {
  try {
    const { meta } = parseScript(fs.readFileSync(process.argv[3], 'utf8'))
    process.stdout.write(JSON.stringify({ ok: true, meta }))
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, error: e.message }))
  }
  process.exit(0)
}

if (process.argv[2] !== 'run') {
  console.error('用法：node wfrun.mjs check <script.js> | run <运行目录>')
  process.exit(2)
}

// ───────── run 模式
const DIR = path.resolve(process.argv[3])
const RUN = readJson(path.join(DIR, 'run.json'))
if (!RUN) {
  console.error(`没有 ${DIR}/run.json`)
  process.exit(2)
}
const STATE_P = path.join(DIR, 'state.json')
const JOURNAL_P = path.join(DIR, 'journal.jsonl')
const old = readJson(STATE_P, {})
const state = {
  id: RUN.id, name: RUN.name, description: RUN.description, phases: RUN.phases || [],
  status: 'running', started: old.started || now(), resumed: old.started ? now() : null, finished: null,
  attempt: (old.attempt || 0) + 1, pid: process.pid, host: os.hostname(),
  logs: [], agents: [], tokens: 0, result_preview: null, error: null,
}
let dirty = true
const touch = () => (dirty = true)
function flushState(force) {
  if (!dirty && !force) return
  dirty = false
  state.tokens = state.agents.reduce((s, a) => s + (a.tokens || 0), 0)
  state.beat = now()
  writeJson(STATE_P, state)
}

// journal：key → {task, done, value, t0, t1, turns, tokens}
const journal = new Map()
for (const line of (fs.existsSync(JOURNAL_P) ? fs.readFileSync(JOURNAL_P, 'utf8') : '').split('\n')) {
  if (!line.trim()) continue
  try {
    const j = JSON.parse(line)
    const cur = journal.get(j.key) || {}
    if (j.k === 'start') journal.set(j.key, { ...cur, task: j.task, round: j.round || 1, base: j.base || null, done: false })
    else if (j.k === 'done') journal.set(j.key, { ...cur, ...j, done: true })
    else if (j.k === 'fail') journal.set(j.key, { ...cur, task: j.task, done: false })
  } catch {
    // 写到一半的行
  }
}
const jwrite = obj => fs.appendFileSync(JOURNAL_P, `${JSON.stringify({ t: now(), ...obj })}\n`)

// ───────── 建任务：短时间内的多个 agent() 攒成一批，一次调用 crew run
let batch = []
let batchTimer = null
let flushing = null // 正在跑的 crew run（取消时要等它建完，再把建出来的一起取消）
let cancelling = false // 不再建新任务（取消中，或脚本已返回在收尾）
let ctlCancel = false // 收到了取消指令：由 checkCtl 收尾退出
function createTask(spec) {
  if (cancelling) return Promise.reject(new Error('工作流已取消'))
  return new Promise((resolve, reject) => {
    batch.push({ spec, resolve, reject })
    if (!batchTimer) batchTimer = setTimeout(flushBatch, 200)
  })
}
function flushBatch() {
  const items = batch
  batch = []
  batchTimer = null
  flushing = (async () => {
    try {
      const out = await sh([CREW_BIN, '--json', 'run', '--stdin', '--owner', RUN.owner || 'workflow'], JSON.stringify({ tasks: items.map(i => i.spec) }))
      const made = JSON.parse(out)
      if (cancelling) made.forEach(m => drop(m.id, 'cancel'))
      items.forEach((it, k) => it.resolve(made[k]))
    } catch (e) {
      items.forEach(it => it.reject(e))
    }
  })()
  return flushing
}

// ───────── 等任务：一个轮询循环读所有在等的任务
const waiters = new Map() // tid → {agent, resolve}
function readTask(tid) {
  return readJson(path.join(TASKS, tid, 'task.json'))
}
function inboxBusy(tid) {
  try {
    return fs.readdirSync(path.join(TASKS, tid, 'inbox')).some(n => n.endsWith('.json'))
  } catch {
    return false
  }
}
function waitTask(tid, a) {
  return new Promise(resolve => waiters.set(tid, { a, resolve }))
}
function usageOf(t) {
  const u = t.usage || {}
  return (u.in || 0) + (u.out || 0)
}
function pollTasks() {
  for (const [tid, w] of waiters) {
    const a = w.a
    const t = readTask(tid)
    if (!t) {
      // 任务目录被删了（crew rm 等）：连续几次读不到就当失败，免得工作流永远等下去
      if (++a.missing >= 5) {
        waiters.delete(tid)
        w.resolve({ ok: false, note: '任务目录不见了（被删除）' })
      }
      continue
    }
    a.missing = 0
    const before = `${a.status}|${a.turns}|${a.tokens}|${a.model}`
    // 续上下文的一步只算这一步自己的回合和 token（任务上的是累计数）
    a.turns = Math.max(0, (t.turns || 0) - (a.base?.turns || 0))
    a.tokens = Math.max(0, usageOf(t) - (a.base?.tokens || 0))
    if (t.model_used) a.model = String(t.model_used)
    if (t.started && !a.t_start) a.t_start = t.started
    if (FINAL_OK.includes(t.status) && (t.round || 1) < (a.wantRound || 1) && !inboxBusy(tid) && now() - (a.dropAt || 0) > 15) {
      // 下一步的指示没被接住（比如投递时任务还没收尾）：重投
      a.redrops = (a.redrops || 0) + 1
      if (a.redrops > 4) {
        waiters.delete(tid)
        w.resolve({ ok: false, note: '续上下文的下一步多次投递都没被接住' })
        continue
      }
      a.dropAt = now()
      drop(tid, 'followup', { text: a.prompt, schema: a.schema || null })
      if (a.redrops > 1) log(`代理 ${a.n}「${a.label}」的下一步没被接住，第 ${a.redrops} 次重投`)
      continue
    }
    if (FINAL_OK.includes(t.status) && (t.round || 1) >= (a.wantRound || 1) && !inboxBusy(tid)) {
      waiters.delete(tid)
      w.resolve({ ok: true, task: t, result: readJson(path.join(TASKS, tid, 'result.json'), {}) })
    } else if (t.status === 'failed') {
      if (inboxBusy(tid)) continue // 上次投的“续跑”还没被处理，别重复投、白占重试次数
      if (a.retries < AUTO_RETRY) {
        a.retries += 1
        a.status = 'retry'
        drop(tid, 'resume', { retry: true })
        log(`代理 ${a.n}「${a.label}」失败（${clip(t.note, 120)}），第 ${a.retries} 次从断点续跑`)
      } else {
        waiters.delete(tid)
        w.resolve({ ok: false, note: t.note || '失败' })
      }
    } else if (FINAL_BAD.includes(t.status)) {
      waiters.delete(tid)
      w.resolve({ ok: false, note: t.note || (t.status === 'cancelled' ? '被取消' : '被否决') })
    } else if (!FINAL_OK.includes(t.status) && (a.status !== 'retry' || t.status === 'running')) {
      a.status = t.status // queued / running / paused / waiting
    }
    if (`${a.status}|${a.turns}|${a.tokens}|${a.model}` !== before) touch()
  }
}

// ───────── 取消
// 停掉所有还没结束的代理：没发出去的批次作废，正在建的建完就取消，在跑的投取消
async function stopAgents() {
  cancelling = true
  if (batchTimer) {
    clearTimeout(batchTimer)
    batchTimer = null
  }
  batch.forEach(i => i.reject(new Error('工作流已取消')))
  batch = []
  try {
    await flushing // 正在建的任务建完后已在 flushBatch 里取消
  } catch {
    // 建失败了：没有要取消的
  }
  for (const tid of waiters.keys()) drop(tid, 'cancel')
  for (const c of procs) {
    try { process.kill(-c.pid, 'SIGTERM') } catch { /* 已经结束 */ }
  }
  let n = 0
  for (const a of state.agents) {
    if (!['done', 'failed', 'cancelled'].includes(a.status)) {
      Object.assign(a, { status: 'cancelled', t_end: now() })
      n += 1
    }
  }
  return n
}

async function checkCtl() {
  const p = path.join(DIR, 'ctl', 'cancel')
  if (ctlCancel || !fs.existsSync(p)) return
  ctlCancel = true
  await stopAgents()
  fs.rmSync(p, { force: true })
  state.status = 'cancelled'
  state.finished = now()
  state.logs.push({ t: now(), msg: '工作流被取消' })
  flushState(true)
  process.exit(0)
}

// ───────── 脚本里的函数
// bash(cmd, opts)：直接跑一条 shell 命令，不经过工人，原样返回 { exit, stdout, stderr, secs }。
// 给确定性的步骤用（自检 / 验收脚本、取清单、落盘检查）：不会抄错输出，也不花 token。
// opts：cwd（默认工作流目录）、timeout_s（默认 1800，到时杀掉、exit = 124）、label（进度日志里显示的名字）。
// 输出各保留最后 2MB。工作流取消时一并杀掉。
const procs = new Set()
function bash(cmd, opts = {}) {
  if (cancelling) return Promise.reject(new Error('工作流已取消'))
  const cwd = opts.cwd ? path.resolve(RUN.cwd || '.', opts.cwd) : (RUN.cwd || process.cwd())
  const limit = 2 * 1024 * 1024
  const t0 = now()
  const label = opts.label || clip(String(cmd).replace(/\s+/g, ' '), 80)
  return new Promise(resolve => {
    const c = spawn('bash', ['-c', String(cmd)], { cwd, detached: true, stdio: ['ignore', 'pipe', 'pipe'], env: process.env })
    procs.add(c)
    let out = '', err = '', killed = false
    const keep = (buf, d) => { buf += d; return buf.length > limit ? buf.slice(-limit) : buf }
    c.stdout.on('data', d => (out = keep(out, d)))
    c.stderr.on('data', d => (err = keep(err, d)))
    const timer = setTimeout(() => {
      killed = true
      try { process.kill(-c.pid, 'SIGKILL') } catch { /* 已经结束 */ }
    }, Math.max(1, Number(opts.timeout_s) || 1800) * 1000)
    const done = code => {
      clearTimeout(timer)
      procs.delete(c)
      const exit = killed ? 124 : (code ?? 1)
      const secs = Math.round((now() - t0) * 10) / 10
      log(`$ ${label} → 退出码 ${exit}${killed ? '（超时杀掉）' : ''}，${secs}s`)
      resolve({ exit, stdout: out, stderr: err, secs })
    }
    c.on('error', e => { err += String(e.message || e); done(127) })
    c.on('close', done)
  })
}

let agentCount = 0
const cap = Number(RUN.concurrency || 0) || Infinity
let active = 0
const slots = []
async function acquire() {
  if (active < cap) {
    active++
    return
  }
  await new Promise(r => slots.push(r))
  active++
}
function release() {
  active--
  slots.shift()?.()
}

function log(msg) {
  state.logs.push({ t: now(), msg: String(msg) })
  if (state.logs.length > 400) state.logs.splice(0, state.logs.length - 400)
  touch()
}

// 模型与思考档位：opts.model / opts.effort（同 Claude Code Workflow），meta.phases[].model / effort，运行时的 model / effort，逐级兜底。
// model 原样传递；profile 单独选择后端，省略 model 时使用 CLI 默认模型。
const PROFILES = ['gpt', 'gpt-chat', 'claude', 'mock']
const isOpenAI = m => /^(gpt|o\d)/i.test(String(m || ''))
function mapModel(m, profile) {
  return [m ? String(m) : null, null]
}
function phaseMeta(title) {
  return state.phases.find(p => p.title === title) || {}
}
// 续上下文：同一个 session 名（或 continueTask 指定的任务）的 agent() 接着同一个工人的对话做下一步；同名的调用排队依次做
const sessions = new Map() // 名字 → 任务 id
const sessionLocks = new Map()
function lockSession(name) {
  const prev = sessionLocks.get(name) || Promise.resolve()
  let release
  const mine = new Promise(r => (release = r))
  sessionLocks.set(name, prev.then(() => mine))
  return prev.then(() => release)
}
const seen = new Map()
let warnedIsolation = false

function makeHooks(ctx) {
  const curPhase = () => ctx.phase
  async function agent(prompt, opts = {}) {
    if (typeof prompt !== 'string' || !prompt.trim()) throw new Error('agent() 的第一个参数要是非空字符串')
    opts = opts || {}
    const n = ++agentCount
    const phase = `${ctx.prefix}${opts.phase ?? curPhase() ?? ''}`
    const label = String(opts.label || prompt.trim().split('\n')[0]).slice(0, 60)
    const sname = opts.continueTask ? `task:${opts.continueTask}` : opts.session ? String(opts.session) : ''
    const ph = phaseMeta(phase)
    const wantProf = opts.profile || RUN.profile || null
    const [mOpt, eOpt] = mapModel(opts.model, wantProf)
    const [mPh, ePh] = mapModel(ph.model, wantProf)
    const [mRun, eRun] = mapModel(RUN.model, wantProf)
    const model = mOpt || mPh || mRun || null
    const effort = opts.effort || eOpt || ph.effort || ePh || RUN.effort || eRun || null
    const sig = JSON.stringify([prompt, opts.schema ?? null, opts.cwd ?? null, opts.files ?? null, opts.accept ?? null, sname, model, effort])
    const h = crypto.createHash('sha1').update(sig).digest('hex').slice(0, 16)
    const occ = (seen.get(h) || 0) + 1
    seen.set(h, occ)
    const key = `${h}#${occ}`
    const profile = opts.profile || (PROFILES.includes(opts.model) && opts.model !== 'claude' ? opts.model : null) || (isOpenAI(opts.model) ? 'gpt' : null) || RUN.profile || undefined
    if (opts.isolation && !warnedIsolation) {
      warnedIsolation = true
      log('crew 不支持 isolation: worktree，按普通目录跑；并行写同一文件的任务请分开目录')
    }
    const a = { n, key, label, phase, session: sname, wantRound: 1, status: 'queued', task: null, cached: false, retries: 0, missing: 0, t_queue: now(), t_start: null, t_end: null, turns: 0, tokens: 0, profile: profile || '', model: [model, effort].filter(Boolean).join('·'), summary: '', note: '' }
    state.agents.push(a)
    touch()
    const unlock = sname ? await lockSession(sname) : () => {}
    await acquire()
    try {
      const j = journal.get(key)
      if (opts.continueTask && !sessions.has(sname)) sessions.set(sname, opts.continueTask)
      if (j?.done) {
        if (sname && j.task) sessions.set(sname, j.task)
        Object.assign(a, { status: 'done', cached: true, task: j.task, t_queue: j.t0, t_start: j.t0, t_end: j.t1, turns: j.turns || 0, tokens: j.tokens || 0, summary: j.summary || '' })
        touch()
        return j.value
      }
      let tid = j?.task
      a.wantRound = j?.round || 1
      if (j?.base) {
        a.base = j.base
        a.cont = true
      }
      Object.defineProperty(a, 'prompt', { value: prompt, enumerable: false })
      Object.defineProperty(a, 'schema', { value: opts.schema || null, enumerable: false })
      const prev = tid ? readTask(tid) : null
      if (!prev || FINAL_BAD.includes(prev.status)) tid = null
      else if (prev.status === 'failed') drop(tid, 'resume', { retry: true })
      if (!tid && sname && sessions.has(sname)) {
        // 接着上一个同名代理的工人做：把这一步作为下一步投给它，上下文（之前读过的、做过的）都还在
        const base = sessions.get(sname)
        const bt = readTask(base)
        if (bt && !FINAL_BAD.includes(bt.status)) {
          tid = base
          a.wantRound = (bt.round || 1) + 1
          a.cont = true
          a.base = { turns: bt.turns || 0, tokens: usageOf(bt) }
          a.t_start = a.dropAt = now()
          // 先记 journal 再投：两步之间崩了，续跑时按轮次判断投没投到（没投到会重投），不会把这一步做两遍
          jwrite({ k: 'start', key, task: tid, round: a.wantRound, base: a.base })
          drop(tid, 'followup', { text: prompt, schema: opts.schema || null })
        } else log(`代理 ${n}「${label}」要续的工人（${base}）已取消或不存在，改开新工人`)
      }
      if (!tid) {
        const origin = { ...(RUN.origin || {}), via: { kind: 'workflow', workflow: RUN.name, run_id: RUN.id, agent_index: n, label } }
        const spec = {
          title: label, prompt,
          accept: opts.accept || '按任务说明完成；结论写进 summary，结构化结果放进 data（有 schema 时），产出文件列进 files。',
          schema: opts.schema || null, cwd: opts.cwd || RUN.cwd, files: opts.files || [], profile, model, effort,
          // 截止日：opts.cutoff，没写就用工作流 args.cutoff（回测项目都带）；opts.cutoff: false 显式不限
          ...((opts.cutoff ?? RUN.args?.cutoff) ? { cutoff: opts.cutoff ?? RUN.args.cutoff } : {}),
          // 成果文件：到 nudge_after 回合（默认 15）还不存在，crew 插话一次让工人先落盘
          ...(opts.deliverable ? { deliverable: opts.deliverable, ...(opts.nudge_after ? { nudge_after: opts.nudge_after } : {}) } : {}),
          group: `wf:${RUN.name}`, owner: RUN.owner, origin, wf: { run: RUN.id, name: RUN.name, n, phase, label },
        }
        const made = await createTask(spec)
        tid = made.id
        jwrite({ k: 'start', key, task: tid })
      }
      if (sname) sessions.set(sname, tid)
      a.task = tid
      touch()
      const r = await waitTask(tid, a)
      a.t_end = now()
      if (!r.ok) {
        a.status = 'failed'
        a.note = clip(r.note, 300)
        jwrite({ k: 'fail', key, task: tid })
        log(`代理 ${n}「${label}」失败，返回 null：${clip(r.note, 160)}`)
        return null
      }
      const res = r.result || {}
      let value
      if (opts.schema) value = res.data ?? null
      else {
        value = String(res.summary || '')
        if ((res.files || []).length) value += `\n\n产出文件：\n${res.files.map(f => `- ${f}`).join('\n')}`
        if ((res.open_issues || []).length) value += `\n\n未解决：\n${res.open_issues.map(x => `- ${x}`).join('\n')}`
      }
      a.status = 'done'
      a.summary = clip(res.summary, 300)
      a.turns = Math.max(0, (r.task.turns || 0) - (a.base?.turns || 0))
      a.tokens = Math.max(0, usageOf(r.task) - (a.base?.tokens || 0))
      jwrite({ k: 'done', key, task: tid, value, t0: a.t_start || a.t_queue, t1: a.t_end, turns: a.turns, tokens: a.tokens, summary: a.summary })
      return value
    } finally {
      release()
      unlock()
      touch()
    }
  }

  async function parallel(thunks) {
    if (!Array.isArray(thunks)) throw new Error('parallel() 要传函数数组：parallel(items.map(x => () => agent(…)))')
    return Promise.all(thunks.map(async (th, i) => {
      try {
        return await (typeof th === 'function' ? th() : th)
      } catch (e) {
        log(`parallel 第 ${i + 1} 项出错，记为 null：${clip(e.message, 200)}`)
        return null
      }
    }))
  }

  async function pipeline(items, ...stages) {
    if (!Array.isArray(items)) throw new Error('pipeline() 第一个参数要是数组')
    return Promise.all(items.map(async (item, i) => {
      let v = item
      for (let s = 0; s < stages.length; s++) {
        try {
          v = await stages[s](v, item, i)
        } catch (e) {
          log(`pipeline 第 ${i + 1} 项在第 ${s + 1} 段出错，记为 null：${clip(e.message, 200)}`)
          return null
        }
      }
      return v
    }))
  }

  function phase(title) {
    ctx.phase = String(title)
    if (!state.phases.some(p => p.title === `${ctx.prefix}${title}`)) state.phases.push({ title: `${ctx.prefix}${title}`, detail: '' })
    touch()
  }

  async function workflow(ref, childArgs) {
    if (ctx.depth > 0) throw new Error('workflow() 只能嵌套一层')
    const p = typeof ref === 'object' && ref?.scriptPath ? path.resolve(RUN.cwd, ref.scriptPath) : findNamed(String(ref))
    if (!p) throw new Error(`找不到工作流：${typeof ref === 'string' ? ref : JSON.stringify(ref)}`)
    const { meta, fn } = parseScript(fs.readFileSync(p, 'utf8'))
    const child = { prefix: `▸ ${meta.name} · `, depth: ctx.depth + 1, phase: '' }
    for (const ph of meta.phases) {
      if (!state.phases.some(p => p.title === `${child.prefix}${ph.title}`)) state.phases.push({ title: `${child.prefix}${ph.title}`, detail: ph.detail || '', model: ph.model || null, effort: ph.effort || null })
    }
    log(`开始子工作流 ${meta.name}`)
    const h = makeHooks(child)
    return fn(h.agent, h.parallel, h.pipeline, h.phase, log, childArgs, budget, h.workflow, meta, bash)
  }

  return { agent, parallel, pipeline, phase, workflow }
}

function findNamed(name) {
  const dirs = [path.join(RUN.cwd || '.', '.claude', 'workflows'), path.join(os.homedir(), '.claude', 'workflows'), path.join(HOME, 'workflow-lib')]
  for (const d of dirs) {
    for (const ext of ['.js', '.mjs']) {
      const p = path.join(d, name + ext)
      if (fs.existsSync(p)) return p
    }
  }
  return null
}

const budget = {
  total: null,
  spent: () => state.agents.reduce((s, a) => s + (a.tokens || 0), 0),
  remaining: () => Infinity,
}

// ───────── 主流程
async function main() {
  const { meta, fn } = parseScript(fs.readFileSync(path.join(DIR, 'script.js'), 'utf8'))
  state.name = meta.name
  state.description = meta.description
  state.phases = meta.phases.map(p => ({ title: p.title, detail: p.detail || '', model: p.model || null, effort: p.effort || null }))
  if (state.attempt > 1) log(`第 ${state.attempt} 次启动：已完成的代理直接复用结果，没跑完的接着等`)
  flushState(true)
  let lastGuard = 0
  const timer = setInterval(() => {
    try {
      void checkCtl()
      pollTasks()
      flushState()
      guardDaemon()
    } catch (e) {
      console.error(e)
    }
  }, 1500)
  // 互相守护：后台服务管运行器（死了就拉起）；运行器反过来盯后台服务的心跳，停了 60 秒就把它拉起来
  // （用户有意 crew down 的不拉）。Claude 关着、后台服务意外退出，工作流也能接着跑。
  function guardDaemon() {
    if (now() - lastGuard < 20) return
    lastGuard = now()
    const beat = readJson(path.join(HOME, 'daemon.json'), null)?.beat || 0
    if (now() - beat < 60 || fs.existsSync(path.join(HOME, 'stopped'))) return
    log('后台服务的心跳停了，拉起它')
    spawn(CREW_BIN, [ 'up', '--watch'], { detached: true, stdio: 'ignore', env: { ...process.env } }).unref()
  }
  const ctx = { prefix: '', depth: 0, phase: '' }
  const h = makeHooks(ctx)
  try {
    const value = await fn(h.agent, h.parallel, h.pipeline, h.phase, log, RUN.args, budget, h.workflow, meta, bash)
    writeJson(path.join(DIR, 'result.json'), { value: value === undefined ? null : value, t: now() })
    state.result_preview = clip(JSON.stringify(value ?? null), 2000)
    state.status = 'done'
    // 没 await 的代理：结果进不了返回值，又没有回合上限会一直跑，取消掉
    const loose = await stopAgents()
    if (loose) log(`脚本已返回，还有 ${loose} 个没 await 的代理没做完，已取消（结果不会进返回值）`)
  } catch (e) {
    state.status = 'failed'
    state.error = clip(e?.stack || String(e), 3000)
    log(`脚本出错：${clip(e?.message || e, 300)}`)
  }
  if (ctlCancel) return // 被取消：checkCtl 收尾退出
  clearInterval(timer)
  state.finished = now()
  flushState(true)
  process.exit(0)
}

process.on('SIGTERM', () => {
  // 后台服务或机器要停：状态留在 running，下次拉起时按 journal 续跑
  flushState(true)
  process.exit(0)
})

main().catch(e => {
  state.status = 'failed'
  state.error = clip(e?.stack || String(e), 3000)
  state.finished = now()
  flushState(true)
  process.exit(1)
})
