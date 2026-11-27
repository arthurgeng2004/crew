# Crew

Crew connects Claude Code with local Codex and Claude subagents plus managed JavaScript workflows. It uses existing CLI logins, keeps task state on your machine, and provides live workflow and content visibility, steering, review, cross-conversation messaging, cache-expiry handling, alerts, and subagent collaboration. Supports Linux and macOS; macOS has been statically reviewed but has not been tested on hardware.

Crew 让 Claude Code 当组长，本机 **Codex 和 Claude 都能作为子代理**：统一派任务、后台运行、插话、提问、验收和退回修改。`/crew` 面板集中显示任务、工作流、执行流程、对话内容和故障；每个任务有独立会话和可恢复的磁盘记录，把组长、子代理、工作流和消息沟通全端打通。

## 主要解决

1. **对话间沟通**：多个 Claude Code 对话可以通过 `message` 互发阶段结论、进展、文件路径和待办，只传必要信息，减少重复背景，节省上下文；`crew peers` 和 `crew msgs` 让本机对话与具体通信内容清晰可查。
2. **Workflow 及子 Agent 无法监控，流程、内容可视化及定制化不足**：`/crew` 面板集中展示工作流阶段、子代理、最近步骤、时间线、原始任务、对话和成果；支持插话、提问、验收、暂停、恢复、重试和取消，并可定制模型档案、并发与工作流。
3. **不同家 AI 无法互通**：Codex 和 Claude 都能作为子代理，由 Claude Code 统一调度，保留各自 CLI 登录和模型能力；子代理之间可共享成果、公告板和消息，不必在不同 AI 工具之间反复切换、复制内容。
4. **缓存过期监控**：监控跨对话缓存状态，对方缓存过期时会拦截发送，改用 `crew peer-log <名字或会话前缀>` 读取上下文，避免消息悄悄断链或基于过期信息继续协作。
5. **托管工作流及子代理任务等**：后台服务托管 JavaScript 工作流和子代理任务，关闭 Claude 对话后仍可继续运行；支持磁盘持久化、断点续跑、结果复用、定时查看、失败重试和验收回路。

**持续更新中。**

## 前提

- macOS 或 Linux，Python **3.9+**，Bash。操作系统附带的 Python 版本可能不足，请以 `python3 --version` 为准。
- 已安装 Claude Code CLI，能运行 `claude` 并已登录 Claude 订阅。
- 已安装 Codex CLI，已运行 `codex login`；`codex login status` 应显示已登录 ChatGPT/Codex 账号。所用版本需支持 `codex app-server` 和动态工具。
- 工作流需要 **Node.js 18+**；不使用工作流时可以不装。
- 插件使用 Claude Code 的 TypeScript hooks / panel 接口。验证版本为 **2.1.332**；若当前 CLI 不支持这些接口，请先更新到支持它们的版本。

默认子代理不需要 API key。它直接使用用户已登录的 Codex，不设置代理或模型提供方，也不覆盖 Codex 默认模型。任务显式传入的 `model` 原样交给 CLI；是否支持由 CLI 和账号决定。

## 安装

将仓库放到一个长期保留的位置：命令链接和插件设置指向这个目录。

```bash
cd crew
chmod +x install.sh uninstall.sh bin/crew workerbin/*
./install.sh
```

脚本检查依赖和 Codex 登录状态，创建 `~/.local/bin/crew` 链接，初始化 `~/.crew/config.json`，追加插件目录到 `~/.claude/settings.json` 的 `env.CLAUDE_CODE_PLUGIN_DIRS`，最后执行 `crew up`。修改现有设置前生成 `settings.json.bak-crew-*` 备份；保留原有插件与其他字段。重复运行不会重复追加。

```bash
./install.sh --no-claude       # 不修改 Claude 设置
./install.sh --no-start        # 安装后暂不启动后台服务
CREW_PYTHON="$(command -v python3)" ./install.sh
BIN_DIR="$HOME/bin" ./install.sh
```

脚本不修改 shell 启动文件。如果命令不在 PATH，可用 `~/.local/bin/crew`，或自行在 shell 配置中加入：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

重新打开 Claude Code 后插件生效。也可临时加载而不修改设置：

```bash
claude --plugin-dir "$PWD/plugin"
```

插件依次查找 `CREW_BIN`、插件目录的 `../bin/crew`、PATH 中的 `crew`。需要自定义时，`CREW_BIN` 填可执行文件路径。

## 第一次使用

```bash
crew run -p mock --wait "测试"       # 不连接模型，走派发 → 待验收
crew ls
crew show <任务ID>
crew review <任务ID> accept
crew wf run examples/wf-mock-ping.js # 需要 Node
```

在 Claude Code 输入 `/crew` 打开面板，再告诉 Claude：“用 crew 派一个任务，计算一个数并写入文件，完成后验收。”默认 `gpt` 档案为本机 Codex；关闭 Claude 对话后后台任务继续运行。唤醒、自动验收提醒和定时查看依赖派发对话仍在运行。

子代理提交 `summary`、`files`、`self_check`、`open_issues`；Claude 读成果后通过、退回修改或否决。退回修改、提问回复和插话会续用原会话。

## 工具和命令

| Claude 工具 | 用途 |
| --- | --- |
| `mcp__crew__dispatch` | 派单个或批量任务，支持共享背景、验收标准、材料 |
| `mcp__crew__status` / `inspect` | 状态、成果、时间线、原始任务和对话 |
| `mcp__crew__review` | accept / revise / reject |
| `mcp__crew__steer` | 插话、回答子代理提问 |
| `mcp__crew__wait` / `control` | 等待、暂停、恢复、重试、取消 |
| `mcp__crew__workflow` | 运行、查看、恢复、取消工作流 |
| `mcp__crew__checkin` | 预计完成时间和定时查看间隔 |
| `mcp__crew__alert` | 查看、解除或删除故障告警 |
| `mcp__crew__message` | 列出同机 Claude 对话，向指定对话发消息 |

表中省略前缀的工具都以 `mcp__crew__` 开头。插件默认只操作本对话派发的任务。

```bash
crew                         # 终端实时面板；crew top 同义
crew help
crew ls --here               # 在 Claude 的 Bash 中看本对话任务
crew show <id>
crew digest <id>             # 完整提交、自查、文件与未解决问题
crew chat <id> -n 0
crew brief <id>
crew log <id> -f
crew say <id> "补充要求"
crew answer <id> "回答"
crew review <id> revise "具体修改意见"
crew review <id> accept
crew pause <id>
crew resume <id> --retry
crew cancel <id>
crew followup <id> "接着做下一步"
crew run -C "$PWD" --model "你的模型名" "任务说明"
crew up
crew down                    # 停服务；下次 up 续跑未完成任务
crew restart
```

终端面板为 **1 主页、2 任务、3 工作流、4 帮助**，按 `1–4` 切页，方向键选中，Enter 进入详情，Esc 返回；帮助页列出插话、验收等按键。鼠标滚轮不操作面板；按住 Shift（macOS 终端通常用 Option）拖选复制。Claude 面板也显示工作流的阶段、子代理及最近步骤。

同一 Claude 对话派发的子代理可用 `team_list / team_read / team_post` 共享成果和公告板；`scope=all` 可读同机其他任务。跨对话消息需要指定目标；对方提示缓存过期时会拦截发送，改用 `crew peer-log <名字或会话前缀>` 读取上下文。`crew peers` 列出可用对话，`crew msgs` 查看消息记录。

## 配置

状态目录默认为 `~/.crew`，可用 `CREW_HOME` 覆盖。配置在 `config.json`，与内置默认值递归合并；列表由用户值整体替换。修改并发、告警等配置后后台服务自动重读；已启动子代理的档案以启动时为准。`CREW_PYTHON` 可覆盖安装时记录的解释器。

```json
{
  "max_parallel": 4,
  "default_profile": "gpt",
  "profiles": {
    "gpt": {
      "backend": "codex",
      "codex_bin": "codex",
      "model": null,
      "effort": null,
      "max_parallel": 4,
      "codex_subagents": false
    },
    "claude": {
      "backend": "claude",
      "claude_bin": "claude",
      "max_parallel": 2,
      "quota_stop": 0.9,
      "explicit_only": true
    }
  },
  "mcp_servers": {},
  "probes": []
}
```

- `max_parallel`：全局同时运行任务数，默认 4；`0` 表示不限。档案同名字段再限制该档案；`parallel_group` 可共享限额。429 会等待和退避，不会自动换为 Claude。
- `model` / `effort`：任务指定优先于档案；默认不覆盖 CLI。`force_effort` 非空时强制覆盖任务思考档位。CLI 实际支持的档位由其模型决定。
- `codex_bin` / `claude_bin`：命令名或可执行文件路径。Codex 的 MCP 由用户在 Codex 自己的配置中管理，Crew 不读取或改写该配置。
- `codex_config` / `codex_skill_roots`：可选的 Codex 启动覆盖与额外 skill 目录，默认不设置提供方、代理或额外目录。启用子代理会绕过 Crew 对任务数的统计，默认关闭。
- `claude`：只有明确使用 `-p claude` / `profile: "claude"` 才启用，使用已登录的 Claude Code 额度。没有额度记录时不阻止派活；有新记录且超过 `quota_stop` 时排队等待重置。
- `defaults.context_tokens` / `tool_output_chars` / `bash_timeout_s`：chat 子代理压缩阈值、工具输出展示长度、单条 shell 默认超时（默认 600 秒，`0` 不限）。Codex 使用自身上下文管理。
- `worker_env` / `worker_env_note`：给子代理的额外环境变量与说明，默认无特定机器依赖。
- `alert_patterns` / `alert_watch` / `probes`：通用故障扫描、重点关注目标、定时健康命令。`probes` 默认空，可配置 `name / key / cmd / every_s / every_fail_s / timeout_s / match`。

可选的 `gpt-chat` 档案使用 OpenAI 兼容的 Chat Completions API，需要单独的 API 额度，不使用 Codex 订阅。设置该档案的 `model`、`base_url`（默认 `https://api.openai.com/v1`）和 `api_key_env`（默认 `OPENAI_API_KEY`）。key 从环境读取，也可放在 `~/.crew/openai.env`，文件仅含变量赋值，权限建议 `600`；自定义位置用 `env_file`。不要把 key 写进仓库。`crew models gpt-chat` 可查看 API 模型并测试请求。

仅 chat 子代理的 MCP 桥读取 Crew 配置中的 `mcp_servers`，默认空；不会寻找未配置的服务。支持 stdio 与 Streamable HTTP：

```json
{
  "mcp_servers": {
    "my-tools": {
      "command": "python3",
      "args": ["/path/to/my_server.py"],
      "description": "我的工具集"
    },
    "my-http": {
      "url": "https://example.invalid/mcp",
      "bearer_token_env": "MY_MCP_TOKEN",
      "enabled": false
    }
  }
}
```

用途来自可选 `description`，或服务握手返回的 `instructions`。HTTP 支持 `headers`、`env_headers`、`bearer_token_env`；stdio 支持 `env`、`cwd`；共同支持 `enabled`、`timeout_s`。密钥只在环境或 `~/.crew/mcp.env`（可用 `mcp_env_file` 改位置）中保存。Crew 提供工具清单、参数校验、文本及图片保存；不提供任何预置服务。

## 托管工作流

脚本以 `meta` 开头，正文为异步 JavaScript，不使用 `import`：

```javascript
export const meta = {
  name: 'demo',
  description: '并行整理后检查文件',
  phases: [{ title: '整理' }, { title: '检查' }],
  concurrency: 2
}
phase('整理')
const results = await parallel(['A', 'B'].map(name => () =>
  agent(`整理示例 ${name}，写入 ${name}.txt 并提交成果`, {
    label: name,
    deliverable: `${name}.txt`,
    nudge_after: 15
  })
))
phase('检查')
const check = await bash('test -f A.txt && test -f B.txt', { timeout_s: 30 })
return { results, check }
```

```bash
crew wf check examples/wf-mock-ping.js
crew wf run examples/wf-mock-ping.js
crew wf ls
crew wf show <id> -v 5
crew wf result <id> --full
crew wf watch <id>
crew wf cancel <id>
crew wf resume <id>
```

`agent(prompt, opts)` 支持 `label / phase / schema / cwd / files / accept / profile / model / effort / deliverable / nudge_after / cutoff`。`schema` 指定提交的 `data` 对象；`session` 同名续用会话，`continueTask` 接着已有任务。默认每个 agent 独立，工作流内不提问组长，提交结果由脚本收下；失败自动续跑最多两次，仍失败返回 `null`，脚本应检查。

其他函数包括 `parallel / pipeline / phase / log / args / budget / workflow`；`workflow` 可组合子脚本。`bash(cmd, {cwd, timeout_s, label})` 直接执行确定性命令，返回 `{exit, stdout, stderr, secs}`；超时退出码 124，取消工作流会停止其命令进程组。后台服务托管运行器，日志和每个 agent 的结果持久化，恢复时复用已完成结果。工作流结束会取消未 await 完成的 agent。

定时查看：派发时传 `eta_min` 和 `check_min`（最多 55 分钟），或用 `checkin` 调整；默认有任务运行时每 45 分钟提醒 Claude 判断是否正常。`/crew auto off` 关闭自动叫醒。自动提醒会消耗 Claude 的用量。

## 故障排查

- 看不到 `/crew`：重开会话，检查 CLI 支持 hooks / panel，以及设置中的插件目录；用 `claude --plugin-dir` 临时验证。用 `CREW_BIN` 指定命令可排查查找问题。
- 子代理启动失败：先运行 `codex login status`、`codex app-server --help`，检查 `codex_bin` 和模型权限；看 `crew log <id>`、`~/.crew/daemon.log` 与 `daemon.out`。
- 429：保持任务等待，Crew 会退避重试。可降低 `max_parallel`，不需要改用其他档案。
- 工作流起不来：检查 `node --version` 至少 18，执行 `crew wf check`，看该工作流的 `runner.log`。
- 告警：`crew alerts`。子代理可用 `report_problem`；脚本可输出 `[CREW-ALERT:source-name] 错误说明`。重要目标立即提示，其他故障按重复次数合并。恢复后通过 `alert` 工具或 `crew alerts --resolve <key> --why "原因"` 解除。
- 终端中文异常：使用已安装的 UTF-8 locale 和支持 Unicode 的终端；`locale -a` 查看本机候选。Python 需带 curses；macOS 不要求存在 `C.UTF-8`。
- 停服务：`crew down`；重新 `crew up` 会续跑。派任务与工作流时也会自动启动后台服务。

更详细的派发、验收、协作规则见 [使用说明](docs/使用说明.md)。

## 卸载与隐私

```bash
./uninstall.sh          # 停服务，移除本安装添加的设置和命令链接；保留状态
./uninstall.sh --purge  # 另删除 CREW_HOME（默认 ~/.crew）中的任务和配置
```

卸载保留其他插件和设置，也保留设置备份；手工管理的插件项不会被本安装认领。确认不再需要后可自行删除仓库目录。安装使用自定义 `CREW_HOME` 时，卸载也应传同一个值。

任务、完整提示词、工具输出、提交文件路径、配置和可选 key 位于本机 `~/.crew`（或 CREW_HOME），不应提交到 Git。Codex 和 Claude 自己的登录、MCP、日志由相应 CLI 管理。Crew 不内置遥测；子代理调用模型和用户配置的外部工具时，相应请求会发给其服务。子代理可以执行本机命令、读写文件；当前后端以非交互模式运行并跳过权限询问，任务说明应明确工作目录和允许修改的范围。
