// 定时查看计划（秒用引擎时钟；every / eta 是分钟）
export type CrewPlan = { every: number; eta: number | null; start: number; next: number; checks: number; set: boolean }

export type CrewTask = {
  id: string
  title: string
  status: string
  round: number
  turns: number
  tokens: number
  elapsed: number
  owner: string
  origin: string
  session: string
  note: string
  question: string | null
  needs_since: number | null
  batch?: string
  wf: string
  step?: string
  step_ago?: number | null
}

// 工作流一行（crew ls --json 的 workflows）
export type CrewWf = {
  id: string
  name: string
  status: string
  owner: string
  session: string
  agents: number
  counts: Record<string, number>
  tokens: number
  elapsed: number
  finished: number | null
  origin: string
}

export type CrewWfAgent = {
  n: number
  label: string
  status: string
  task: string | null
  cached: boolean
  cont: boolean
  retries: number
  turns: number
  tokens: number
  elapsed: number
  summary: string
  note: string
  model?: string
  step?: string
  step_ago?: number | null
}

export type CrewWfPhase = {
  title: string
  detail: string
  status: 'done' | 'running' | 'pending'
  done: number
  total: number
  failed: number
  elapsed: number
  agents: CrewWfAgent[]
}

// 工作流进度树（crew wf show --json）
export type CrewWfTree = {
  wf: CrewWf
  description: string
  phases: CrewWfPhase[]
  logs: { t: number; msg: string }[]
  result_preview: string | null
  error: string | null
  attempt: number
}

// 来回对话里的一条（crew chat）：who = claude / worker / you / system；tools = 合并后的“执行了 N 次命令”
export type CrewChatItem = {
  who: 'claude' | 'worker' | 'you' | 'system'
  label: string
  head: string
  text: string
  t: number | null
  tools?: boolean
}

export type CrewChat = {
  id: string
  items: CrewChatItem[]
}

// 故障告警：工人遇到 / 报告、或巡检发现数据源坏了（crewlib/core.py raise_alert）
export type CrewAlert = {
  key: string
  title: string
  status: 'open' | 'resolved' | 'quiet'
  first: number
  last: number
  resolved: number | null
  resolved_why?: string
  count: number
  tasks: string[]
  owners: string[]
  sessions: string[]
  source: string
  important: boolean
  sample: string
}

export type CrewSnap = {
  daemonUp: boolean
  tasks: CrewTask[]
  workflows: CrewWf[]
  alerts?: CrewAlert[]
  error: string
}

export type CrewDetail = {
  id: string
  lines: string[]
}

// 派活上下文（crew brief）：谁派的、工人收到的第一条消息原文、之后的追加指示
export type CrewBrief = {
  key: string
  id: string
  origin_text: string
  session: string
  profile: string
  cwd: string
  opening: string
  later: string[]
}

declare module 'claude-code' {
  interface PluginState {
    crew: {
      snap: CrewSnap
      lead: boolean
      auto: boolean
      keep: boolean
      plans: Record<string, CrewPlan>
      selected: string
      detail: CrewDetail
      brief: CrewBrief
      tab: 'chat' | 'brief' | 'log'
      chat: CrewChat
      confirmRm: string
      scope: 'mine' | 'all'
      notified: string[]
      paneOpen: boolean
      mode: 'home' | 'tasks' | 'wf'
      conv: string
      wfSel: string
      wfTree: CrewWfTree | null
      agentSel: string
      agentLog: CrewDetail
      bandWfs: CrewWfTree[]
    }
  }
}
