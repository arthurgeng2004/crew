# crew：Claude 带队，便宜的模型干活

**crew** 是一个 [Claude Code](https://claude.com/claude-code) 插件加一个后台服务。Claude 当组长：拆任务、写验收标准、验收、做判断。大量采集、整理、跑脚本、写初稿的活交给后台"工人"去做，工人可以是 DeepSeek，也可以是任何 OpenAI 兼容接口上的模型。

> English summary: crew lets Claude Code act as a team lead that dispatches work to cheaper LLM "workers" (DeepSeek or any OpenAI-compatible model) running in a background daemon. Workers have bash, file tools and optional MCP access. Claude reviews results (accept / revise / reject) and is woken up automatically when workers finish, ask questions or fail. Hosted workflows (`agent()` / `parallel()` / `pipeline()` scripts) keep running even after Claude exits. UI strings are in Chinese.

```
 你 ──── /crew 面板 · crew 终端面板 ─────────┐
                                            │ 看 · 插话 · 回答 · 验收 · 暂停/取消
 Claude（组长）── mcp__crew__* 工具 ──→ crew 后台服务 ──→ 工人 × N（DeepSeek / GPT / …）
      ↑                                       │（bash / 读写文件 / MCP / 提问 / 提交）
      └──── 插件在工人需要组长时自动唤醒 ───────┘
```

## 为什么用它

- **省 Claude 的 token**：Claude 只写任务说明和看凝练后的成果摘要（≤600 字），工人的长对话不进 Claude 的上下文。
- **Claude 关了也照常跑**：后台服务独立于 Claude 进程。关掉 Claude、断开 SSH 都不影响；做完会叫醒派活的那个对话。
- **有验收回路**：成果先进「待验收」；退回修改时，意见交给同一个工人，它带着全部上下文接着改。
- **托管工作流**：写法同 Claude Code 的 Workflow 脚本，每个 `agent()` 是一个工人；中断后按 journal 从断点续跑。
- **能中途干预**：在 Claude 里、`/crew` 面板或终端里都能插话、回答提问、暂停、取消、重试。
- **故障会上报**：数据源或 MCP 坏了（5xx、连不上、鉴权或额度错误），crew 识别出来并叫醒受影响的对话，不会把"服务坏了"当成"没有数据"。

## 依赖

| 依赖 | 用途 | 必需 |
|---|---|---|
| Python 3.8+ | 后台服务、工人、命令行（只用标准库） | 是 |
| Claude Code（支持 `register.tsx` 模块的插件） | 组长端：工具、面板、自动唤醒 | 是 |
| 一个 OpenAI 兼容接口的 API key | 工人用的模型（默认 DeepSeek 官方 API） | 是 |
| Node.js 18+ | 托管工作流（`crew wf`）的运行器 | 用工作流才需要 |
| [Codex CLI](https://github.com/openai/codex)（已登录） | `gpt` 档案直接用本机已登录的 Codex，不用 key | 可选 |
| Pillow | `view_image` 转图 | 可选 |

只支持 Linux / macOS（用到了 `fcntl`、`curses`）。

## 安装

```bash
git clone https://github.com/<你的账号>/crew.git ~/crew
cd ~/crew
bash install.sh
```

`install.sh` 做这几件事：

1. 把 `bin/crew` 链接到 `~/.local/bin/crew`。
2. 生成 `~/.crew/config.json`。
3. 询问 API key，写进 `~/.crew/deepseek.env`（以及可选的 `~/.crew/openai.env`，只有 `gpt-chat` 用），权限 600，**不进仓库**。
4. 在 `~/.claude/settings.json` 的 `env` 里加上 `CLAUDE_CODE_PLUGIN_DIRS=<仓库>/plugin`，让新开的 Claude Code 会话加载插件。原文件备份为 `settings.json.bak-crew`。
5. 启动后台服务（`crew up`）。

可选参数：`--no-key` 不问 key，`--no-claude` 不改 Claude 设置。也可以手动装：链接 `bin/crew`，自己写 key 文件，启动 Claude 时用 `--plugin-dir <仓库>/plugin`。

验证：

```bash
crew models ds                 # 列出接口上的模型，并按档案设置试发一条
crew run -p mock "测试"         # mock 档案不调模型，走一遍状态机
crew                           # 打开终端面板
```

然后新开一个 Claude Code 会话，输入 `/crew`。

### 密钥放哪里

key 只从档案里的 `env_file`（一行 `KEY=VALUE`）或同名环境变量读，**不写进 config.json，也不进仓库**。`.gitignore` 已经排除了 `*.env` 和 `.crew/`。

```bash
echo 'DEEPSEEK_API_KEY=sk-...' > ~/.crew/deepseek.env && chmod 600 ~/.crew/deepseek.env
```

## 在 Claude 里用

- `/crew`：打开面板，进入组长模式（往系统提示里加一段组长守则）。
- 直接对 Claude 说"这批采集派给工人做"，它会用 `mcp__crew__dispatch` 派任务。
- 工人提交、提问或失败时，插件等 Claude 空闲后自动提交一条 `[crew] …` 提示。Claude 去验收或回答，然后继续推进。同一批任务都做完才叫一次，成果摘要直接附在消息里。
- `/crew parallel`（例如 `/crew parallel gpt 2`）看或改并发，和命令行 `crew parallel` 一样。
- `/crew auto off` 关掉自动唤醒；`/crew keep off` 关掉定时查看；`/crew off` 退出组长模式（工具仍可用）；`/crew help` 看使用说明。

Claude 的工具：

| 工具 | 用途 |
|---|---|
| `dispatch` | 派一批任务（共同的 `context` / `accept` / `files` / `cwd` 写在顶层，各任务写自己的部分；可选 `profile`、`model`、`effort`、`schema`、`deliverable`、`cutoff`） |
| `status` / `inspect` | 列任务和工作流；看成果摘要、任务原文、时间线、原始对话 |
| `review` | accept / revise（意见具体到条目，工人接着改）/ reject；`ids` 一次验收多个 |
| `steer` | 回答提问，或给运行中的工人插话 |
| `control` | 暂停 / 继续 / 取消 / 失败重试（从断点续跑） |
| `wait` | 阻塞等到有任务要处理（平时用不着，插件会唤醒） |
| `workflow` | 托管工作流：run / show / result / list / cancel / resume |
| `checkin` | 设定 / 修改定时查看的间隔和预计完成时间 |
| `alert` | 故障告警：list / resolve / delete |
| `message` | 给本机另一个开着的 Claude 对话发消息 |

**结果不串对话**：每个任务记着是哪个对话派的。叫醒只发给那个对话，别的对话不能操作它的任务。

## 在终端里用

```bash
crew                     # 面板：1 主页 · 2 工作流 · 3 任务 · 4 帮助；Enter 打开，Esc 返回，q 退出
crew ls                  # 列任务（在 Claude 的 Bash 里加 --here 只看本对话的）
crew show <id>           # 详情：来源、验收标准、成果、自查、未解决问题、历次验收
crew chat <id>           # 来回对话（-n 0 看全部）
crew log <id> -f         # 跟随时间线
crew say <id> "改用 FY2025 口径"      # 插话
crew answer <id> "A"                 # 回答提问
crew review <id> revise "第 3 行没有来源" -s 5
crew run -t 标题 -a "验收标准" -C ~/proj "任务说明"    # 自己派任务（不会叫醒 Claude）
crew up | down | restart              # 后台服务
crew rm <id> | --failed
crew models <档案>                    # 检查一个档案的接口
crew parallel                         # 看并发设置和正在跑的数量
crew parallel 8                       # 全局最多同时 8 个工人（0 = 不限）
crew parallel gpt 2                   # 单个档案（同名额组的档案一起改）
crew parallel wf 3                    # 工作流里同时跑的 agent() 默认上限
crew effort ds low                    # 改档案的默认思考档位
crew hours gpt 09:30-01:00            # 档案的可用时段（UTC），off 取消
crew alerts                           # 故障告警
```

id 可以只写末尾几位。推荐 tmux 布局：左边 `claude`，右边 `crew`。

## 托管工作流

Claude（或你自己）写好脚本交给 crew 跑。运行器（`crewlib/wfrun.mjs`，Node）由后台服务拉起，和 Claude 进程无关。

```js
export const meta = { name: 'demo', description: '并行算两个数再汇总', phases: [{ title: '并行' }, { title: '汇总' }] }
phase('并行')
const nums = await parallel([3, 4].map(k => () => agent(`用 python3 计算 ${k} 的 10 次方，写进 pow${k}.txt`, {
  label: `${k}^10`, schema: { type: 'object', properties: { value: { type: 'integer' } }, required: ['value'] } })))
phase('汇总')
const r = await bash('cat pow3.txt pow4.txt')      // 确定性的步骤直接跑命令，不经过工人
return { nums, files: r.stdout }
```

- 全局函数：`agent / parallel / pipeline / phase / log / bash / args / workflow`；纯 JS，不能 import。
- `agent(prompt, opts)`：`label`、`phase`、`schema`、`profile`、`model`、`effort`、`cwd`、`files`、`accept`、`deliverable` + `nudge_after`、`cutoff`、`session`（同名接着同一个工人，保留上下文）、`continueTask`（接着一个已有任务做）。
- 默认每个 `agent()` 是一个全新工人，看不到别的代理和 Claude 的对话。失败自动续跑 2 次，仍失败返回 `null`。
- `bash(cmd, { cwd, timeout_s, label })` 返回 `{ exit, stdout, stderr, secs }`。
- 并发：脚本 `meta.concurrency`，或运行时 `--concurrency N`（Claude 的 `workflow` 工具同名参数），都没写就用配置 `wf_concurrency`；0 = 不限。
- 运行：`crew wf run 脚本.js [--args JSON] [--cwd 目录] [--concurrency N]`；`crew wf ls | show <id> -v 3 | watch <id> | result <id> | cancel <id> | resume <id>`。
- 例子见 `examples/`（`wf-mock-ping.js` 不调模型）。

## 配置：`~/.crew/config.json`

第一次运行时自动生成，未写的项用内置默认值（`crewlib/core.py` 的 `DEFAULT_CONFIG`）。常改的：

### 模型档案 `profiles`

档案 = 一个接口 + 模型 + 参数。派活时写 `profile` 选档案，不写就用 `default_profile`（默认 `ds`）。

```jsonc
{
  "default_profile": "ds",
  "models": {
    // 别名 → 模型 id；efforts = 支持的思考档位（[] = 不发 reasoning_effort）；window = 上下文窗口
    "deepseek": { "id": "deepseek-chat", "efforts": [], "window": 131072 },
    "gpt": { "id": "gpt-5", "efforts": ["minimal", "low", "medium", "high"] }
  },
  "profiles": {
    "ds": {
      "base_url": "https://api.deepseek.com/v1",
      "api_key_env": "DEEPSEEK_API_KEY",
      "env_file": "~/.crew/deepseek.env",
      "model": "deepseek",
      "max_tokens": 8192,
      "vision": false,              // 模型能看图就开，工人会多一个 view_image 工具
      "fallback_profile": null,     // 被限流（429）时改派到哪个档案，如 "gpt"
      "price_per_mtok": null        // {"in":…, "out":…, "cached":…} 填了就显示成本
    },
    // 接别的模型：照抄一个档案，改 base_url / model / api_key_env。例如本地 vLLM：
    "local": { "base_url": "http://127.0.0.1:8000/v1", "api_key_env": "LOCAL_KEY", "model": "Qwen/Qwen3-32B", "direct": true }
  }
}
```

其他档案字段：`temperature`、`max_tokens_field`（推理模型写 `max_completion_tokens`）、`context_tokens`（超过就压缩上下文）、`max_parallel` + `parallel_group`（并发名额）、`direct`（不走环境变量里的代理）、`headers`、`extra_body`、`stream`、`hours`（可用时段）、`models`（非空时只允许这些模型）、`backend`（`codex` / `mock`）。

### GPT / Codex（可选）

**`gpt` 档案直接用本机已登录的 Codex**：ChatGPT 账号登录（`codex login`）或 API key 登录都行，crew 不读也不存密钥。派活写 `profile: "gpt"` 即可。

- 每个任务起一个 `codex app-server`，工人拿到 Codex 原生的 shell、apply_patch、view_image、MCP、网页搜索、skills 和自动压缩；crew 的 `submit_result` / `ask_lead` / `report_problem` 作为动态工具挂进去。
- 模型、网关、MCP 都读 `~/.codex/config.toml`。档案 `model` 默认是 `null`，用 Codex 自己的默认模型；要固定就写模型 id（ChatGPT 账号登录时，有些模型 id 会被拒，用 Codex 里能选的那些）。
- 用的是你 ChatGPT 账号的 Codex 额度；`max_parallel`（默认 4）控制同时跑几个，`hours` 可以限定时段。
- 需要较新的 Codex CLI（用到 app-server 的 experimentalApi / dynamicTools）。`codex_bin` 可以换成你自己的包装脚本。
- 没装 Codex：派 `gpt-chat`，crew 自己的工具循环直接调 OpenAI 接口，key 写在 `~/.crew/openai.env`。

### MCP 数据服务（可选）

给工人 `mcp_tools` / `mcp_call` 两个工具，调用前按 schema 校验参数。只支持 streamable HTTP：

```jsonc
{
  "mcp_servers": {
    "filings": { "url": "http://127.0.0.1:8011/mcp", "purpose": "SEC 文件清单和原文" },
    "search":  { "url": "https://example.com/mcp", "headers": { "Authorization": "Bearer ${SEARCH_TOKEN}" } }
  },
  "mcp_from_claude_json": false   // true = 另外读 ~/.claude.json 里的 HTTP mcpServers
}
```

`headers` 里的 `${ENV}` 运行时从环境变量替换，密钥不用写进配置。高级用法：`mcp_bridge` 指向你自己的桥脚本（提供 `server_urls()` 和 `_rpc(server, method, params, timeout, url=None)`）。

### 并发（全部可调）

| 层级 | 配置字段 | 默认 | 怎么改 |
|---|---|---|---|
| 全局：同时跑几个工人 | `max_parallel` | 0（不限） | `crew parallel 8` |
| 单个档案 | `profiles.<名>.max_parallel` | ds 不限，gpt / gpt-chat 4 | `crew parallel gpt 2` |
| 名额组：几个档案共用一份名额 | `profiles.<名>.parallel_group` | gpt 与 gpt-chat 同组 | `crew parallel ds group cheap` |
| 工作流里同时跑的 `agent()` | `wf_concurrency` | 0（不限） | `crew parallel wf 3`；单个工作流 `meta.concurrency` 或 `--concurrency` |

0 都表示不限。后台服务每 10 秒重读配置，改完不用重启。调小时已经在跑的不会被打断，等跑完一个再补一个。接口被限流（429）时工人带抖动地退避等待，不算失败；API 额度紧就把并发调小。

### 其他

| 字段 | 默认 | 说明 |
|---|---|---|
| `force_effort` | null | 全局强制思考档位 |
| `bash_timeout_s` | 600 | 工人单条命令超时，0 = 不限 |
| `worker_env` | {} | 工人命令的额外环境变量（如 `PIP_INDEX_URL`） |
| `worker_env_note` | … | 写进工人守则的环境说明，换机器时改这里 |
| `alert_patterns` / `alert_watch` | … | 什么样的输出算故障、哪些地址出故障立即叫醒 |
| `probes` | [] | 巡检命令：定期跑，退出码非 0 开告警，恢复自动解除 |

工人的 `python3` / `pip` 走 `workerbin/`：设了 `CREW_PYTHON`（比如一个装好 pandas 的 conda 环境）就用它，否则用系统的 `python3`。取数脚本出错时打印一行 `[CREW-ALERT:源名] 说明` 并以非 0 退出，crew 就会上报故障。

## 状态和文件

全部状态是文件，放在 `$CREW_HOME`（默认 `~/.crew`）：

```
config.json                 配置
tasks/<id>/task.json        任务规格与状态（只有后台服务写）
tasks/<id>/msgs.json        工人与模型的完整对话
tasks/<id>/events.jsonl     时间线
tasks/<id>/result.json      成果（历次 result.r1.json …）
tasks/<id>/inbox/           外部投递：插话、回答、暂停、验收（后台服务消费）
workflows/<id>/             工作流脚本、journal、进度
```

任务状态：排队 → 运行 →（提问 ⇄ 运行）→ 待验收 → 通过 / 退回修改（第 2 轮）/ 否决；另有暂停、失败、取消。

更新代码：插件改完在 Claude 里 `/reload-plugins`；`crewlib/` 改完后台服务会在空闲时自动换新，急用 `crew restart`（运行中的任务续跑）。

## 安全提示

- **工人没有沙箱**：它的 bash 以后台服务的用户身份运行，命令不需要确认，权限和你的 shell 一样。派任务时写清工作目录和只许写的文件；最好用专门的用户或容器跑。
- key 只放在 `~/.crew/*.env` 或环境变量里。提交代码前确认没有把 `~/.crew` 或 `.env` 文件拷进仓库。
- 跨对话消息（`message`）只发给本机开着的 Claude 会话；收到的消息会注明"不是用户本人"。

## 测试

```bash
claude plugin validate plugin
claude plugin test plugin                    # 插件：派发、唤醒、面板、工作流、告警、定时查看等 14 项
CREW_HOME=/tmp/crew-test crew run -p mock "[ask] 测试"   # mock 档案不调模型，走一遍状态机（[ask] 会先提问）
CREW_HOME=/tmp/crew-test crew wf run examples/wf-mock-ping.js
```

## 目录

```
bin/crew              命令行入口
crewlib/              后台服务（daemon）、工人（worker、codexrun）、命令行（cli）、终端面板（tui）、工作流（wf、wfrun.mjs）、MCP（mcp、mcp_bridge）
plugin/               Claude Code 插件（hooks/register.tsx）和测试
workerbin/            工人 PATH 最前面的 python3 / pip 包装
examples/             工作流例子
install.sh            安装脚本
```
