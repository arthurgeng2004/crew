import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const ME = 'claude:sess-123'
const TASKS = [
  { id: '1124-0000-aaa', title: '整理示例文档', status: 'review', round: 1, turns: 12, tokens: 120000, elapsed: 300, owner: ME, origin: '对话 sess-123「测试」', session: 'sess-1234567890', note: '', profile: 'gpt', question: null, needs_since: 1 },
  { id: '1124-0000-bbb', title: '整理示例目录', status: 'waiting', round: 1, turns: 5, tokens: 50000, elapsed: 120, owner: ME, origin: '对话 sess-123「测试」', session: 'sess-1234567890', note: '', profile: 'gpt', question: '用哪个版本？', needs_since: 2 },
  { id: '1124-0000-ddd', title: '脚本派的失败任务', status: 'failed', round: 1, turns: 80, tokens: 900000, elapsed: 900, owner: 'script', origin: 'script', session: '', note: '接口连续失败', profile: 'gpt', question: null, needs_since: 4 },
  { id: '1124-0000-ccc', title: '别的会话的任务', status: 'review', round: 1, turns: 3, tokens: 9000, elapsed: 60, owner: 'claude:other', origin: '对话 other000「别的对话」', session: 'other000', note: '', profile: 'gpt', question: null, needs_since: 3 },
]

let WFS: Record<string, unknown>[] = []
let ALERTS: Record<string, unknown>[] = []
const CLAIMED = new Set<string>()
let TASKS_X: Record<string, unknown>[] = []
const TREE = {
  wf: { id: 'wf-1124-0000-w01', name: 'sweep-demo', status: 'running', owner: ME, session: 'sess-1234567890', agents: 3, counts: { done: 1, running: 1, queued: 1 }, tokens: 50000, elapsed: 120, finished: null, origin: '对话「测试」' },
  description: '三个示例并行整理', attempt: 1, error: null, result_preview: null, logs: [{ t: 1, msg: '拿到 1 个' }],
  phases: [{ title: '采集', detail: '一家一个工人', status: 'running', done: 1, total: 3, failed: 0, elapsed: 120, agents: [
    { n: 1, label: '示例甲', status: 'done', task: '1124-0000-t01', cached: false, cont: false, retries: 0, turns: 20, tokens: 30000, elapsed: 90, summary: '找到两份披露', note: '' },
    { n: 2, label: '示例乙', status: 'running', task: '1124-0000-t02', cached: false, cont: false, retries: 0, turns: 8, tokens: 20000, elapsed: 60, summary: '', note: '', step: 'bash: python3 fetch.py', step_ago: 4 },
    { n: 3, label: '示例丙', status: 'queued', task: null, cached: false, cont: false, retries: 0, turns: 0, tokens: 0, elapsed: 0, summary: '', note: '' },
  ] }],
}

type World = { calls: string[][]; stdin: string[]; submits: string[]; sends: string[]; toasts: string[] }

function base(on: On, env: Record<string, string> = { CREW_BIN: '/opt/crew' }) {
  mock.env(on, env)
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('command.register', async ($, e) => ({ value: { command: e.name } }))
  on('tool.register', async ($, e) => ({ value: { tool: `mcp__crew__${e.name}` } }))
  on('turn.start', async ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', async () => ({ text: '' }))
  on('ui.close', async () => ({ value: undefined }))
}

function fake(on: On, env?: Record<string, string>): World {
  CLAIMED.clear()
  const w: World = { calls: [], stdin: [], submits: [], sends: [], toasts: [] }
  base(on, env)
  on('session.id', async () => ({ value: 'sess-1234567890' }))
  on('session.cwd', async () => ({ value: '/tmp/crew-demo' }))
  on('session.root', async () => ({ value: '/tmp/crew-demo' }))
  on('ui.open', async () => ({ value: { isPlaced: true as const } }))
  on('prompt.compose', async () => ({ sections: [] }))
  on('session.send', async ($, e) => {
    w.sends.push(`${JSON.stringify(e.to)} ${e.text}`)
    return JSON.stringify(e.to).includes('gone') ? { isDelivered: false as const, reason: '那个会话没有在运行' } : { isDelivered: true as const }
  })
  on('ui.toast', async ($, e) => {
    w.toasts.push(e.text)
    return { value: undefined }
  })
  on('prompt.submit', async ($, e) => {
    w.submits.push(e.text)
    return { text: e.text }
  })
  on('process.run', async ($, e) => {
    const argv = [...e.argv]
    w.calls.push(argv)
    if (e.init?.stdin) {
      w.stdin.push(e.init.stdin)
    }
    const a = argv.slice(argv.indexOf('--json') >= 0 ? argv.indexOf('--json') + 1 : 1)
    let out: unknown = { ok: true, id: a[1], status_before: 'waiting' }
    if (a[0] === 'help') out = 'crew 使用说明…'
    if (a[0] === 'peers') out = [
      { name: 'me', session: 'sess-1234567890', status: 'busy', topic: '本对话', me: true, crew_tasks: 1, crew_workflows: 0, crew_active: 0, cache_warm: true, idle_s: 0 },
      { name: 'warm-1', session: 'warm0001-x', status: 'idle', topic: '存储建模', me: false, crew_tasks: 3, crew_workflows: 1, crew_active: 1, cache_warm: true, idle_s: 600 },
      { name: 'cold-1', session: 'expired-demo-x', status: 'idle', topic: '开源版', me: false, crew_tasks: 2, crew_workflows: 0, crew_active: 0, cache_warm: false, idle_s: 5820, context_tokens: 313931, cwd: '/opt/crew-demo', transcript: '/tmp/claude/projects/demo/expired-demo-x.jsonl' },
    ]
    if (a[0] === 'claim') {
      const keys = a.slice(a.indexOf('--owner') + 2).filter(k => k !== '--release')
      out = a.includes('--release') ? keys : keys.filter(k => !CLAIMED.has(k))
      if (!a.includes('--release')) keys.forEach(k => CLAIMED.add(k))
    }
    if (a[0] === 'digest') out = a.slice(1).filter(x => x.startsWith('1124')).map(id => `${id}「成果」结论：做完了`).join('\n\n')
    if (a[0] === 'ls') out = { daemon: { alive: true, host: 'h', pid: 1 }, tasks: [...TASKS, ...TASKS_X], workflows: WFS, alerts: ALERTS }
    if (a[0] === 'wf' && a[1] === 'run') out = { id: 'wf-1124-0000-w01', name: 'sweep-demo', phases: ['采集'] }
    if (a[0] === 'wf' && a[1] === 'show') out = TREE
    if (a[0] === 'wf' && a[1] === 'ls') out = WFS
    if (a[0] === 'wf' && a[1] === 'result') out = '工作流 wf-1124-0000-w01「sweep-demo」完成 · 返回值：3'
    if (a[0] === 'run') out = [{ id: '1124-0001-new', title: '新任务', profile: 'gpt' }]
    if (a[0] === 'log') out = ['03:00:00 ● 运行 → 待验收', '03:00:01 📦 提交第 1 轮：完成']
    if (a[0] === 'wait') out = { ready: [TASKS[0]], pending: [], timed_out: false }
    if (a[0] === 'chat') out = { id: a[1], items: [
      { who: 'claude', label: 'Claude', head: '派活', text: '任务：整理示例文档\n从文档提取章节', t: 1 },
      { who: 'worker', label: '工人', head: '', text: '我先读 文档 第 7 段', t: 2 },
      { who: 'worker', label: '工人', head: '', text: '（执行了 3 次命令：bash ×3）', t: null, tools: true },
      { who: 'you', label: '你', head: '插话', text: '用 第二版', t: 3 },
    ] }
    if (a[0] === 'brief') out = { id: a[1], origin_text: '派发来源：对话「测试」　会话 sess-1234567890', profile: 'gpt', cwd: '/tmp/crew-demo', opening: '任务：整理示例文档\n派发方：Claude 对话「测试」\n\n从文档提取章节', later: ['03:00:02 ↳ 插话（Claude）：用 第二版'] }
    const stdout = a[0] === 'show' ? '任务详情文本' : typeof out === 'string' ? out : JSON.stringify(out)
    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return w
}

const START = { cwd: '/tmp/crew-demo', surface: 'terminal' as const, isInteractive: true }

test('dispatch 派发到本会话名下并打开组长模式', async ($, on) => {
  const w = fake(on)
  mock.clock(on)
  await $.session.start(START)
  const r = await $.tool.call({ tool: 'mcp__crew__dispatch', tool_use_id: 't1', tasks: [{ title: 'a', prompt: 'b', accept: 'c' }] } as never)
  expect(String(r.result)).toContain('已派 1 个')
  const run = w.calls.find(c => c.includes('run'))
  expect(run).toContain(ME)
  expect(w.stdin[0]).toContain('"cwd":"/tmp/crew-demo"')
  expect(w.stdin[0]).toContain('"origin":{"session":"sess-1234567890","cwd":"/tmp/crew-demo"}')
  const composed = await $.prompt.compose({ model: 'm', promptModel: 'm', surfaces: ['terminal'], tools: [], outputStyle: null, traits: [] } as never)
  expect(composed.sections.map(s => s.id)).toContain('crew-lead')
})

test('workflow 代理派的任务：派发来源写明 workflow 运行与第几个代理；Bash 里跑 crew 也带上', async ($, on) => {
  const w = fake(on)
  mock.clock(on)
  const bash: string[] = []
  on('agent.spawn', async () => ({ model: 'm', agentId: 'ag-1' }))
  on('tool.call', { tool: 'Bash' }, async ($, e) => {
    bash.push(String((e as { command?: string }).command))
    return { result: 'ok' }
  })
  await $.session.start(START)
  await $.agent.spawn({
    tool_use_id: 'wf1', prompt: 'p', description: 'collect:demo', subagentType: 'demo-collector', provider: { plugin: 'engine', tier: 'core' },
    parentModel: 'm', background: true, fork: false, workflow: { runId: 'wf_abc-123', agentIndex: 3 },
  } as never)
  await $.tool.call({ tool: 'mcp__crew__dispatch', tool_use_id: 't7', agentId: 'ag-1', tasks: [{ title: 'a', prompt: 'b', accept: 'c' }] } as never)
  expect(w.stdin.at(-1)).toContain('"via":{"kind":"workflow","run_id":"wf_abc-123","agent_index":3,"label":"collect:demo"')
  await $.tool.call({ tool: 'Bash', tool_use_id: 't8', agentId: 'ag-1', command: 'crew run X' } as never)
  await $.tool.call({ tool: 'Bash', tool_use_id: 't9', agentId: 'ag-1', command: 'ls' } as never)
  await $.tool.call({ tool: 'Bash', tool_use_id: 't10', command: 'crew ls' } as never)
  expect(bash[0]).toMatch(/^export CREW_VIA='\{"kind":"workflow".*'; crew run X$/)
  expect(bash.slice(1)).toEqual(['ls', 'crew ls'])
})

test('工人需要组长时自动唤醒，只唤醒一次，只管本会话的任务', async ($, on) => {
  const w = fake(on)
  const clock = mock.clock(on)
  await $.session.start(START)
  await clock.advance(3000)
  await clock.settle()
  expect(w.submits).toHaveLength(1)
  expect(w.submits[0]).toContain('1124-0000-aaa')
  expect(w.submits[0]).toContain('用哪个版本')
  expect(w.submits[0]).not.toContain('1124-0000-ccc')
  await clock.advance(9000)
  await clock.settle()
  expect(w.submits).toHaveLength(1)
})

test('Claude 忙时不唤醒，空下来再唤醒', async ($, on) => {
  const w = fake(on)
  const clock = mock.clock(on)
  await $.session.start(START)
  await $.turn.start({ text: 'x', turnId: 'u1' } as never)
  await clock.advance(6000)
  await clock.settle()
  expect(w.submits).toHaveLength(0)
  await $.turn.complete({ answer: '', durationMs: 1, isAborted: false, turnId: 'u1' } as never)
  await clock.settle()
  expect(w.submits).toHaveLength(1)
})

test('review / steer / control / wait 走对命令行', async ($, on) => {
  const w = fake(on)
  mock.clock(on)
  await $.session.start(START)
  const rv = await $.tool.call({ tool: 'mcp__crew__review', tool_use_id: 't2', id: '1124-0000-aaa', verdict: 'revise', feedback: '第 3 行没有来源', score: 5 } as never)
  expect(String(rv.result)).toContain('退回修改')
  expect(w.calls.find(c => c.includes('review'))).toEqual(expect.arrayContaining(['review', '1124-0000-aaa', 'revise', '第 3 行没有来源', '-s', '5']))
  const st = await $.tool.call({ tool: 'mcp__crew__steer', tool_use_id: 't3', id: '1124-0000-bbb', message: '用 第二版' } as never)
  expect(String(st.result)).toContain('回答')
  await $.tool.call({ tool: 'mcp__crew__control', tool_use_id: 't4', id: '1124-0000-aaa', action: 'retry' } as never)
  expect(w.calls.find(c => c.includes('resume'))).toContain('--retry')
  const wt = await $.tool.call({ tool: 'mcp__crew__wait', tool_use_id: 't5', timeout_s: 10 } as never)
  expect(String(wt.result)).toContain('需要处理')
})

test('命令行出错时把原因交给模型', async ($, on) => {
  base(on)
  mock.clock(on)
  on('session.id', async () => ({ value: 'sess-1234567890' }))
  on('process.run', async () => ({ value: { exitCode: 1, stdout: '', stderr: '找不到唯一的任务：zzz', isStdoutTruncated: false, isStderrTruncated: false } }))
  await $.session.start(START)
  const r = await $.tool.call({ tool: 'mcp__crew__inspect', tool_use_id: 't6', id: 'zzz' } as never)
  expect(r.isError).toBe(true)
  expect(String(r.result)).toContain('找不到唯一的任务')
})

test('/crew 打开面板，面板在各个界面都画得出来', async ($, on) => {
  fake(on)
  const clock = mock.clock(on)
  await $.session.start(START)
  const out = await $.command.run({ command: 'crew', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 200 } } as never)
  expect(String(out.text)).toContain('crew 面板已打开')
  await clock.advance(3000)
  await clock.settle()
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({
      plugin: 'crew', surface, component: 'Pane', requestId: 'crew',
      props: { title: 'crew', isFocused: false, bodyColumns: 70, placement: 'dock' } as never,
      viewport: { columns: 200, rows: 40 } as never,
    })
    // 打开是主页：工作流和对话派的代理两个入口
    expect(await ui.find({ text: /^工作流$/ })).toBeDefined()
    expect(await ui.find({ text: /^任务$/ })).toBeDefined()
    expect(await ui.find({ text: /别的对话/ })).toBeDefined()
    await ui.press({ key: 'nav-tasks' })
    await clock.settle()
    expect(await ui.find({ text: /整理示例文档/ })).toBeDefined()
    expect(await ui.find({ key: 'say' })).toBeDefined()
    // 默认显示对话：Claude 派的活、工人的回复、你的插话，各自带标签；工具调用合成一行
    expect(await ui.find({ text: /^Claude：$/ })).toBeDefined()
    expect(await ui.find({ text: /^工人：$/ })).toBeDefined()
    expect(await ui.find({ text: /^你：$/ })).toBeDefined()
    expect(await ui.find({ text: /我先读 文档 第 7 段/ })).toBeDefined()
    expect(await ui.find({ text: /执行了 3 次命令/ })).toBeDefined()
    // 派活详情：派发来源、工人收到的任务原文、追加指示
    await ui.press({ key: 'tab-brief' })
    await clock.settle()
    expect(await ui.find({ text: /派发来源：对话「测试」/ })).toBeDefined()
    expect(await ui.find({ text: /插话（Claude）：用 第二版/ })).toBeDefined()
    await ui.press({ key: 'tab-chat' })
    await clock.settle()
    // 默认只列本对话派的；点“看全部对话”才列别的对话的，并标出来源
    expect(await ui.find({ text: /别的会话的任务/ })).toBeUndefined()
    await ui.press({ key: 'scope' })
    await clock.settle()
    expect(await ui.find({ text: /别的对话/ })).toBeDefined()
    await ui.press({ key: 'scope' })
    await clock.settle()
    await ui.press({ key: 'nav-home' })
    await clock.settle()
    await ui.unmount()
  }
  const top = await $.command.run({ command: 'crew', args: 'top', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 200 } } as never)
  expect(String(top.text)).toContain('使用说明')
})

test('交给 Claude 处理：投到派这个任务的对话，不投本对话；失败任务可以确认后删除', async ($, on) => {
  const w = fake(on)
  const clock = mock.clock(on)
  await $.session.start(START)
  await $.turn.start({ text: 'x', turnId: 'u1' } as never)      // 忙着：不触发自动唤醒，只看按钮
  await $.command.run({ command: 'crew', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 200 } } as never)
  await clock.advance(3000)
  await clock.settle()
  const ui = await $.ui.mount({
    plugin: 'crew', surface: 'terminal', component: 'Pane', requestId: 'crew',
    props: { title: 'crew', isFocused: true, bodyColumns: 120, placement: 'dock' } as never,
    viewport: { columns: 200, rows: 60 } as never,
  })
  // 别的对话派的：发到那个对话（先进任务页，切到看全部对话）
  await ui.press({ key: 'nav-tasks' })
  await clock.settle()
  await ui.press({ key: 'scope' })
  await clock.settle()
  await ui.press({ key: 't-1124-0000-ccc' })
  await clock.settle()
  await ui.press({ key: 'ask' })
  await clock.settle()
  expect(w.sends.at(-1)).toContain('other000')
  expect(w.sends.at(-1)).toContain('1124-0000-ccc')
  expect(w.submits.join('\n')).not.toContain('1124-0000-ccc')
  // 不是从对话派的：不交，只提示
  await ui.press({ key: 't-1124-0000-ddd' })
  await clock.settle()
  await ui.press({ key: 'ask' })
  await clock.settle()
  expect(w.toasts.at(-1)).toContain('不是从 Claude 对话派的')
  expect(w.submits.join('\n')).not.toContain('1124-0000-ddd')
  // 失败任务：按两次才删
  await ui.press({ key: 'rm' })
  await clock.settle()
  expect(w.calls.some(c => c.includes('rm'))).toBe(false)
  expect(await ui.find({ text: /确认删除/ })).toBeDefined()
  await ui.press({ key: 'rm' })
  await clock.settle()
  expect(w.calls.find(c => c.includes('rm'))).toEqual(expect.arrayContaining(['rm', '1124-0000-ddd']))
  // 本对话派的：在本对话提交
  await ui.press({ key: 't-1124-0000-aaa' })
  await clock.settle()
  await ui.press({ key: 'ask' })
  await clock.settle()
  expect(w.submits.at(-1)).toContain('1124-0000-aaa')
  await ui.unmount()
})

test('工作流：交给 crew 托管、主页点进进度树、点代理看最近几步、结束时唤醒并给凝练结果', async ($, on) => {
  const w = fake(on)
  const clock = mock.clock(on)
  WFS = [TREE.wf]
  await $.session.start(START)
  await $.turn.start({ text: 'x', turnId: 'u1' } as never)
  const r = await $.tool.call({ tool: 'mcp__crew__workflow', tool_use_id: 'w1', action: 'run', script: 'export const meta = { name: "sweep-demo", description: "d" }', args: { n: 3 } } as never)
  expect(String(r.result)).toContain('wf-1124-0000-w01')
  expect(w.calls.find(c => c.includes('wf'))).toEqual(expect.arrayContaining(['wf', 'run', '--stdin', '--owner', ME]))
  expect(w.stdin.at(-1)).toContain('"args":{"n":3}')
  expect(w.stdin.at(-1)).toContain('"session":"sess-1234567890"')
  await $.command.run({ command: 'crew', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 200 } } as never)
  await clock.advance(3000)
  await clock.settle()
  const ui = await $.ui.mount({
    plugin: 'crew', surface: 'terminal', component: 'Pane', requestId: 'crew',
    props: { title: 'crew', isFocused: true, bodyColumns: 120, placement: 'dock' } as never,
    viewport: { columns: 200, rows: 60 } as never,
  })
  expect(await ui.find({ text: /sweep-demo/ })).toBeDefined()
  await ui.press({ key: 'hw-wf-1124-0000-w01' })
  await clock.advance(3000)
  await clock.settle()
  expect(await ui.find({ text: /示例乙/ })).toBeDefined()
  expect(await ui.find({ text: /bash: python3 fetch\.py/ })).toBeDefined()
  expect(await ui.find({ text: /找到两份披露/ })).toBeDefined()
  await ui.press({ key: 'a-1' })
  await clock.advance(3000)
  await clock.settle()
  expect(w.calls.some(c => c.includes('log') && c.includes('1124-0000-t01'))).toBe(true)
  expect(await ui.find({ text: /提交第 1 轮/ })).toBeDefined()
  await ui.unmount()
  // 结束：空闲时唤醒一次，带凝练结果
  WFS = [{ ...TREE.wf, status: 'done', finished: Date.now() / 1000 }]
  await $.turn.complete({ turnId: 'u1' } as never)
  await clock.advance(3000)
  await clock.settle()
  const wake = w.submits.find(s => s.includes('工作流结束'))
  expect(wake).toContain('返回值：3')
  await clock.advance(6000)
  await clock.settle()
  expect(w.submits.filter(s => s.includes('工作流结束'))).toHaveLength(1)
  WFS = []
})

test('数据源坏了：工人遇到后立刻唤醒派活的对话（优先于验收），恢复后再说一声；别的对话的故障不叫', async ($, on) => {
  const w = fake(on)
  const clock = mock.clock(on)
  const al = { key: 'example.invalid:9001', title: '示例服务不可用：502', status: 'open', first: 100, last: 200, resolved: null, count: 3,
    tasks: ['1124-0000-aaa'], owners: [ME], sessions: ['sess-1234567890'], source: '工具输出', important: true, sample: '[CREW-ALERT] 示例服务不可用：502 from http://example.invalid:9001' }
  ALERTS = [al, { ...al, key: 'other-host', title: '别人的故障', owners: ['claude:other'], sessions: ['other000'], tasks: ['1124-0000-ccc'] },
    { ...al, key: 'probe-only', title: '巡检发现没人撞上的故障', owners: [], sessions: [], tasks: [], source: '巡检' }]
  await $.session.start(START)
  await clock.advance(3000)
  await clock.settle()
  expect(w.submits[0]).toContain('⚠ 故障')
  expect(w.submits[0]).toContain('1124-0000-aaa')
  expect(w.submits[0]).not.toContain('别人的故障')
  expect(w.submits[0]).not.toContain('巡检发现没人撞上的故障')
  await clock.advance(12000)
  await clock.settle()
  expect(w.submits.filter(s => s.includes('⚠ 故障'))).toHaveLength(1)
  ALERTS = [{ ...al, status: 'resolved', resolved: Date.now() / 1000, resolved_why: '巡检「示例服务」恢复正常' }]
  await clock.advance(3000)
  await clock.settle()
  expect(w.submits.some(s => s.includes('已恢复') && s.includes('1124-0000-aaa'))).toBe(true)
  ALERTS = []
})

test('同一次派发的一批：都做完才叫一次，成果摘要直接带上；动别的对话的任务要带 --as 本对话', async ($, on) => {
  const w = fake(on)
  const clock = mock.clock(on)
  const mk = (id: string, status: string) => ({ ...TASKS[0], id, status, batch: 'b-1', title: `批量${id.slice(-1)}`, needs_since: status === 'review' ? 5 : null })
  TASKS_X = [mk('1124-0000-x01', 'review'), mk('1124-0000-x02', 'running')]
  await $.session.start(START)
  await clock.advance(3000)
  await clock.settle()
  // 先叫的是本对话原有的那批（aaa 待验收、bbb 提问）；x01 的同批 x02 还在跑，不叫
  expect(w.submits.join('\n')).not.toContain('1124-0000-x01')
  TASKS_X = [mk('1124-0000-x01', 'review'), { ...mk('1124-0000-x02', 'review'), needs_since: 6 }]
  await clock.advance(12000)
  await clock.settle()
  const wake = w.submits.find(s => s.includes('1124-0000-x01'))
  expect(wake).toContain('1124-0000-x02')
  expect(wake).toContain('结论：做完了')
  expect(w.submits.filter(s => s.includes('1124-0000-x01'))).toHaveLength(1)
  await $.tool.call({ tool: 'mcp__crew__review', tool_use_id: 'r1', ids: ['1124-0000-x01', '1124-0000-x02'], verdict: 'accept', feedback: 'ok' } as never)
  const rv = w.calls.filter(c => c.includes('review'))
  expect(rv).toHaveLength(2)
  expect(rv.every(c => c.includes('--as') && c.includes(ME))).toBe(true)
  TASKS_X = []
})

test('输入框上方的进度栏：本对话的工作流画成 阶段 → 代理 的树，任务逐个列出；别的对话的不出现；不再用状态栏', async ($, on) => {
  const w = fake(on)
  const clock = mock.clock(on)
  const status: (string | undefined)[] = []
  on('ui.status', async ($_, e) => { status.push((e as { text?: string }).text); return { value: undefined } })
  WFS = [TREE.wf]
  await $.session.start(START)
  await clock.advance(3000)
  await clock.settle()
  const ui = await $.ui.mount({
    plugin: 'crew', surface: 'terminal', component: 'AbovePrompt',
    props: { hasSurvey: false, isCollapsed: false, isWorking: false, maxRows: 30, bodyColumns: 150 } as never,
    viewport: { columns: 160, rows: 50 } as never,
  })
  expect(await ui.find({ text: /^crew$/ })).toBeDefined()
  expect(await ui.find({ text: /sweep-demo/ })).toBeDefined()
  expect(await ui.find({ text: /代理 1\/3/ })).toBeDefined()
  expect(await ui.find({ text: /^采集$/ })).toBeDefined()
  expect(await ui.find({ text: /^示例乙$/ })).toBeDefined()
  expect(await ui.find({ text: /bash: python3 fetch\.py/ })).toBeDefined()
  expect(await ui.find({ text: /^整理示例文档$/ })).toBeDefined()
  expect(await ui.find({ text: /用哪个版本/ })).toBeDefined()
  expect(await ui.find({ text: /别的会话的任务/ })).toBeUndefined()
  expect(status.every(s => s === undefined)).toBe(true)
  await ui.unmount()
  WFS = []
  void w
})

test('定时查看（没定计划）：有活在跑、45 分钟没请求就提醒一次；没进展 / 回合过多 / 跑满 2 小时就请 Claude 去查；没活不提醒', async ($, on) => {
  const w = fake(on)
  const clock = mock.clock(on)
  WFS = [{ ...TREE.wf, elapsed: 600 }]
  TASKS_X = [{ id: '1124-0000-t02', title: '示例乙', status: 'running', round: 1, turns: 40, tokens: 400000, elapsed: 600, owner: ME, wf: 'wf-1124-0000-w01', origin: 'x', session: 'sess-1234567890', note: '', profile: 'gpt', question: null, needs_since: null }]
  CLAIMED.add('x')
  await $.session.start(START)
  await clock.advance(3000)
  await clock.settle()
  const before = w.submits.length
  await clock.advance(44 * 60000)
  await clock.settle()
  expect(w.submits.slice(before).some(s => s.includes('定时查看'))).toBe(false)
  await clock.advance(60000 + 3000)
  await clock.settle()
  const first = w.submits.filter(s => s.includes('定时查看'))
  expect(first).toHaveLength(1)
  expect(first[0]).toContain('没发现异常')
  expect(first[0]).toContain('（默认）')
  expect(first[0]).toContain('sweep-demo')
  // 这一轮 Claude 回了「继续等」
  await $.turn.start({ text: first[0], turnId: 'k1' } as never)
  await $.turn.complete({ turnId: 'k1' } as never)
  // 又 45 分钟：工作流没进展、代理回合过 150、累计跑满 2 小时
  WFS = [{ ...TREE.wf, elapsed: 7300 }]
  TASKS_X = [{ ...TASKS_X[0], turns: 160 }]
  await clock.advance(45 * 60000 + 3000)
  await clock.settle()
  const second = w.submits.filter(s => s.includes('定时查看'))
  expect(second).toHaveLength(2)
  expect(second[1]).toContain('自动检查发现')
  expect(second[1]).toContain('没有进展')
  expect(second[1]).toContain('160 回合')
  expect(second[1]).toContain('已跑 2h')
  // 工作流结束后不再提醒
  await $.turn.start({ text: 'x', turnId: 'k2' } as never)
  await $.turn.complete({ turnId: 'k2' } as never)
  WFS = []
  TASKS_X = []
  await clock.advance(50 * 60000)
  await clock.settle()
  expect(w.submits.filter(s => s.includes('定时查看'))).toHaveLength(2)
})

test('定时查看（Claude 定计划）：工作流写 eta_min / check_min 按它提醒；checkin 改间隔；超过预计时间点出来', async ($, on) => {
  const w = fake(on)
  const clock = mock.clock(on)
  WFS = []
  TASKS_X = []
  await $.session.start(START)
  await $.turn.start({ text: 'x', turnId: 'u1' } as never)
  const r = await $.tool.call({ tool: 'mcp__crew__workflow', tool_use_id: 'w1', action: 'run', script: 'export const meta = { name: "sweep-demo", description: "d" }', eta_min: 60, check_min: 20 } as never)
  expect(String(r.result)).toContain('20 分钟后')
  WFS = [{ ...TREE.wf, elapsed: 60 }]
  await $.turn.complete({ turnId: 'u1' } as never)
  await clock.advance(19 * 60000)
  await clock.settle()
  expect(w.submits.filter(s => s.includes('定时查看'))).toHaveLength(0)
  await clock.advance(60000 + 3000)
  await clock.settle()
  const first = w.submits.filter(s => s.includes('定时查看'))
  expect(first).toHaveLength(1)
  expect(first[0]).toContain('预计 60 分钟，每 20 分钟看一次')
  // Claude 判断正常，把下次改成 10 分钟后
  await $.turn.start({ text: first[0], turnId: 'k1' } as never)
  const c = await $.tool.call({ tool: 'mcp__crew__checkin', tool_use_id: 'c1', id: 'wf-1124-0000-w01', check_min: 10 } as never)
  expect(String(c.result)).toContain('10 分钟后')
  await $.turn.complete({ turnId: 'k1' } as never)
  WFS = [{ ...TREE.wf, elapsed: 90 * 60, tokens: 900000, counts: { done: 2, running: 1 } }]
  await clock.advance(10 * 60000 + 3000)
  await clock.settle()
  const second = w.submits.filter(s => s.includes('定时查看'))
  expect(second).toHaveLength(2)
  expect(second[1]).toContain('超过预计的 60 分钟')
  expect(second[1]).not.toContain('没有进展')
  const list = await $.tool.call({ tool: 'mcp__crew__checkin', tool_use_id: 'c2' } as never)
  expect(String(list.result)).toContain('wf-1124-0000-w01：预计 60 分钟，每 10 分钟')
  WFS = []
})

test('跨对话消息：对方缓存已过期就不发，改为告诉发件人用 peer-log 直接读它的上下文；缓存在的照常送达', async ($, on) => {
  const w = fake(on)
  mock.clock(on)
  await $.session.start(START)
  const one = await $.tool.call({ tool: 'mcp__crew__message', tool_use_id: 'm1', to: 'cold-1', text: '我改了你的 README' } as never)
  expect(String(one.result)).toContain('没发')
  expect(String(one.result)).toContain('crew peer-log cold-1')
  expect(String(one.result)).toContain('/opt/crew-demo')
  expect(w.sends.length).toBe(0)
  expect(w.stdin.some(x => x.includes('cache_expired'))).toBe(true)
  const all = await $.tool.call({ tool: 'mcp__crew__message', tool_use_id: 'm2', to: 'crew', text: 'crew 有更新' } as never)
  expect(String(all.result)).toContain('已送达')
  expect(String(all.result)).toContain('没发')
  expect(w.sends.length).toBe(1)
  expect(w.sends[0]).toContain('warm0001-x')
  const ls = await $.tool.call({ tool: 'mcp__crew__message', tool_use_id: 'm3' } as never)
  expect(String(ls.result)).toContain('缓存已过期（闲置 97 分钟')
})

test('面板导航为主页 / 任务 / 工作流；已结束按 finished（缺失用 updated）倒序，需处理仍在前', async ($, on) => {
  fake(on)
  TASKS_X = [
    { ...TASKS[0], id: 'final-old', title: '较早结束', status: 'accepted', finished: 100, updated: 999 },
    { ...TASKS[0], id: 'final-new', title: '较晚结束', status: 'rejected', finished: 200, updated: 200 },
    { ...TASKS[0], id: 'final-fallback', title: '缺少结束时间', status: 'cancelled', finished: null, updated: 300 },
  ]
  const clock = mock.clock(on)
  await $.session.start(START)
  await $.command.run({ command: 'crew', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 200 } } as never)
  await clock.advance(3000)
  await clock.settle()
  // 全部对话四个需处理任务始终领先；有限的行数逐个揭示结束任务的顺序。
  for (const rows of [22, 24, 26]) {
    const ui = await $.ui.mount({
      plugin: 'crew', surface: 'terminal', component: 'Pane', requestId: 'crew',
      props: { title: 'crew', isFocused: true, bodyColumns: 120, placement: 'dock' } as never,
      viewport: { columns: 200, rows } as never,
    })
    await ui.press({ key: 'nav-tasks' })
    await clock.settle()
    if (rows === 22) {
      await ui.press({ key: 'scope' })
      await clock.settle()
    }
    for (const id of ['aaa', 'bbb', 'ccc', 'ddd']) {
      expect(await ui.find({ key: `t-1124-0000-${id}` })).toBeDefined()
    }
    expect(await ui.find({ key: 't-final-fallback' })).toBeDefined()
    if (rows >= 24) expect(await ui.find({ key: 't-final-new' })).toBeDefined()
    else expect(await ui.find({ key: 't-final-new' })).toBeUndefined()
    if (rows >= 26) expect(await ui.find({ key: 't-final-old' })).toBeDefined()
    else expect(await ui.find({ key: 't-final-old' })).toBeUndefined()
    await ui.press({ key: 'nav-wf' })
    await clock.settle()
    expect(await ui.find({ text: /没有工作流/ })).toBeDefined()
    await ui.press({ key: 'nav-home' })
    await clock.settle()
    await ui.unmount()
  }
  TASKS_X = []
})

for (const route of ['override', 'bundled', 'path'] as const) {
  test(`crew 命令查找：${route}`, async ($, on) => {
    const w = fake(on, route === 'override' ? { CREW_BIN: '/opt/custom-crew' } : {})
    const checked: string[] = []
    on('fs.exists', async ($, e) => {
      checked.push(e.path)
      return { value: route === 'bundled' }
    })
    mock.clock(on)
    await $.session.start(START)
    await $.tool.call({ tool: 'mcp__crew__dispatch', tool_use_id: `bin-${route}`, model: 'user-custom-model', tasks: [{ prompt: 'p' }] } as never)
    const run = w.calls.find(c => c.includes('run'))!
    if (route === 'override') {
      expect(run[0]).toBe('/opt/custom-crew')
      expect(checked.length).toBe(0)
    } else if (route === 'bundled') {
      expect(run[0]).toMatch(/\/plugin\/\.\.\/bin\/crew$/)
      expect(checked.some(p => p.endsWith('/bin/crew'))).toBe(true)
    } else {
      expect(run[0]).toBe('crew')
      expect(checked.length).toBeGreaterThan(0)
    }
    expect(w.stdin.some(x => x.includes('"model":"user-custom-model"'))).toBe(true)
  })
}
