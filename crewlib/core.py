"""crew 的共享状态层：任务目录、配置、事件流、投递箱。

一切状态都是文件，放在 $CREW_HOME（默认 ~/.crew）：
  config.json                 模型档案、并发、默认预算
  daemon.json                 后台服务心跳（pid / 主机 / 时间）
  tasks/<id>/task.json        任务规格与状态（只有后台服务写）
  tasks/<id>/msgs.json        工人与模型的完整对话（只有工人线程写）
  tasks/<id>/events.jsonl     时间线：每回合、每次工具调用、每次状态变化（面板读它）
  tasks/<id>/result.json      工人提交的成果
  tasks/<id>/inbox/*.json     外部投递：引导消息、回答、暂停、取消、验收（后台服务消费）
  tasks/<id>/toolout/*.txt    超长工具输出的全文
外部（命令行、面板、Claude 插件）只投递文件，从不直接改 task.json，所以没有写冲突。
"""
import fcntl
import glob
import hashlib
import json
import os
import random
import re
import socket
import string
import sys
import time
import unicodedata

HOME = os.path.abspath(os.path.expanduser(os.environ.get("CREW_HOME") or "~/.crew"))
TASKS = os.path.join(HOME, "tasks")
CTL = os.path.join(HOME, "ctl")

ACTIVE = ("queued", "running", "paused", "waiting")
NEEDS_LEAD = ("review", "waiting", "failed")
FINAL = ("accepted", "rejected", "cancelled")
STATUS_ZH = {
    "queued": "排队", "running": "运行", "paused": "暂停", "waiting": "提问",
    "review": "待验收", "accepted": "通过", "rejected": "否决", "failed": "失败", "cancelled": "取消",
}

DEFAULT_CONFIG = {
    "max_parallel": 0,          # 全局同时跑几个工人，0 = 不限：派多少跑多少（crew parallel 8 改）
    "wf_concurrency": 0,        # 工作流里同时跑几个 agent() 的默认上限，0 = 不限（脚本 meta.concurrency、run --concurrency 优先）
    "default_profile": "ds",
    # 全局强制思考档位（设了就压过一切，任务里写的 effort 也不管用）。默认不设：按档案 / 任务里写的。
    "force_effort": None,
    # 模型别名：任务 / 工作流 / 档案里写 model: "<别名>"（或完整模型 id）。
    # efforts = 该模型支持的思考档位；window = 上下文窗口（含回复）；可写 fallback: {model, effort}：没开通时自动改用。
    "models": {
        "deepseek": {"id": "deepseek-chat", "efforts": [], "window": 131072},
        "gpt": {"id": "gpt-5", "efforts": ["minimal", "low", "medium", "high"]},
    },
    # 上下文多长时压缩（按 token 计，用接口返回的实际用量校准）。任务 / 档案的 context_tokens 优先；
    # 都没写就按模型窗口：窗口 − 回复上限 − 4 万余量。再没有窗口信息才用这里的默认。
    "defaults": {"context_tokens": 300000},
    "profiles": {
        # 默认工人：任何 OpenAI 兼容的 chat/completions 接口都行（DeepSeek 官方、火山方舟、硅基流动、vLLM……）。
        # 密钥从 env_file（一行 KEY=VALUE）或同名环境变量读取，不写进配置。
        "ds": {
            "base_url": "https://api.deepseek.com/v1",
            "api_key_env": "DEEPSEEK_API_KEY",
            "env_file": "~/.crew/deepseek.env",
            # 被限流（429）时改派给哪个档案；运行中的连续 429 超过 fallback_after_s 秒换档案接着做。None = 不改派
            "fallback_profile": None,
            "fallback_after_s": 180,
            "model": "deepseek",
            "effort": None,               # 默认思考档位；派活 / 工作流里写 effort 可以按任务改
            "temperature": 0.3,
            "max_tokens": 8192,           # 回复上限（推理 + 回答）
            "request_timeout_s": 900,
            "retries": 6,
            "vision": False,              # 模型能看图就设 True：给工人 view_image
            "price_per_mtok": None,       # {"in":…, "out":…, "cached":…}，每百万 token 单价，填了就显示成本
        },
        "ds-fast": {
            "base_url": "https://api.deepseek.com/v1",
            "api_key_env": "DEEPSEEK_API_KEY",
            "env_file": "~/.crew/deepseek.env",
            "fallback_profile": None,
            "fallback_after_s": 180,
            "model": "deepseek",
            "effort": None,
            "temperature": 0.2,
            "max_tokens": 4096,
            "request_timeout_s": 600,
            "retries": 6,
            "vision": False,
            "price_per_mtok": None,
        },
        # GPT 工人（可选）：跑在本机已登录的 Codex 里（ChatGPT 账号登录或 API key 登录都行，crew 不碰密钥）。
        # 每个任务起一个 codex app-server，拿到 Codex 原生的 shell / apply_patch / MCP / skills / 上下文压缩；
        # 模型、网关、MCP 都读 ~/.codex/config.toml。model 为 None = 用 Codex 的默认模型；要固定就写模型 id。
        # 没装 Codex 就用下面的 gpt-chat（直接调 OpenAI 接口，要 API key）。
        "gpt": {
            "model": None,
            "effort": None,                                # None = Codex 的默认思考档位
            "temperature": None,
            "max_tokens": 32768,
            "max_tokens_field": "max_completion_tokens",   # 推理模型不认 max_tokens
            "efforts": ["minimal", "low", "medium", "high"],
            "models": [],                                  # 非空时只允许派这些模型（派活写别的会报错）
            "context_tokens": 200000,
            "max_parallel": 4,                             # 同组（parallel_group）同时最多几个，超出的排队
            "parallel_group": "gpt",
            "request_timeout_s": 900,
            "retries": 6,
            "vision": True,
            "price_per_mtok": None,
            "backend": "codex",
            "codex_bin": "codex",
            "codex_skill_roots": ["~/.crew/codex-skills"],   # 额外 skills 目录
            "codex_subagents": False,                        # Codex 子代理会绕过并发名额，默认关
            # 可用时段（UTC，可跨午夜，如 "09:30-01:00"）：时段外新任务排队，运行中的到点暂停、下个时段续跑
            "hours": None,
        },
        # 不用 Codex：crew 自己的 chat/completions 工具循环
        "gpt-chat": {
            "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "env_file": "~/.crew/openai.env", "direct": False,
            "model": "gpt", "effort": "high", "temperature": None, "max_tokens": 32768, "max_tokens_field": "max_completion_tokens",
            "efforts": ["minimal", "low", "medium", "high"], "models": [],
            "context_tokens": 200000, "max_parallel": 4, "parallel_group": "gpt", "request_timeout_s": 900, "retries": 6, "vision": True,
        },
        # 不调模型，走一遍状态机（测试用）
        "mock": {"backend": "mock"},
    },
    "tool_output_chars": 8000,
    # 故障告警：工具输出里出现这些特征 = 数据源 / 服务坏了（不是“没有数据”）。crew 记下来、提示工人、唤醒派活的 Claude 对话。
    # [名字, 正则]；正则不分大小写。脚本可以直接打印一行 [CREW-ALERT] 说明 来报告。
    "alert_patterns": [
        ["脚本报告故障", r"^\[CREW-ALERT(?::[\w.-]+)?\][^\n]*"],
        ["上游服务 5xx", r"UPSTREAM_5XX|upstream_status\"?\s*:\s*5\d\d|HTTP Error 5\d\d|\b5\d\d (Bad Gateway|Service Unavailable|Gateway Time-?out|Internal Server Error)|\b5\d\d from https?://"],
        ["连不上服务", r"Connection refused|ECONNREFUSED|服务连不上|Failed to establish a new connection|Name or service not known|No route to host|Temporary failure in name resolution"],
        ["密钥或额度", r"invalid[_ ]api[_ ]key|insufficient_quota|quota exceeded|ModelNotOpen|AuthenticationError"],
    ],
    # 这些地址（子串）出故障一律叫醒 Claude（你的数据接口、本机 MCP）；别的地址同一故障出现 3 次或波及 2 个任务才叫
    "alert_watch": ["127.0.0.1", "localhost", "mcp-"],
    # 工人的 MCP 数据服务（mcp_tools / mcp_call）。{名字: {"url": "http://…/mcp", "purpose": "一句话用途", "headers": {…}}}；
    # 只支持 streamable HTTP。为空且 mcp_from_claude_json 为 false 时不给工人这两个工具。
    "mcp_servers": {},
    "mcp_from_claude_json": False,  # True = 另外读 ~/.claude.json 里 url 是 http(s) 的 mcpServers
    # 自定义桥（高级）：一个 Python 文件，提供 server_urls() -> {名字: url} 和 _rpc(server, method, params, timeout, url=None) -> (应答, 错误)
    "mcp_bridge": None,
    "mcp_timeout_s": 180,
    # 巡检：后台服务定期跑这些命令，退出码非 0 = 故障（开告警），恢复后自动解除并通知。
    # 例：{"name": "行情库", "key": "quotes", "cmd": "python3 ~/bin/quotes_ping.py", "every_s": 600, "every_fail_s": 180, "timeout_s": 120, "match": ["quotes"]}
    "probes": [],
    "bash_timeout_s": 600,      # 工人单条 bash 命令默认 10 分钟超时（0 = 不限）；工人可传 timeout_s 放宽
    # 工人跑命令时的环境：PATH 前面加 crew/workerbin（python3 / pip 指向 CREW_PYTHON 或系统 python3），再加这些环境变量
    "worker_env": {},
    # 写进工人守则的“环境说明”：换机器时只改这里
    "worker_env_note": (
        "环境：\n"
        "- 命令不需要任何确认、没有沙箱；只写任务允许写的位置。\n"
        "- 缺 Python 库就 pip install。网络请求自己加超时（curl -m、requests timeout），连两次不上就换来源，不要反复重试。\n"
        "- 没有回合上限：一直做到任务真正完成，再调用 submit_result 交给组长。"
    ),
}


# 思考档位从低到高。DeepSeek 只有 none / low / high / max，GPT 有 minimal / low / medium / high（新款还有 none、xhigh）：
# 写哪个都行，落到模型支持的最近一档（同样近取高的），所以 medium 给 DeepSeek 是 high、max 给 GPT 是它的最高档
EFFORT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
EFFORT_ALIAS = {"off": "none"}


def fit_effort(effort, supported=None):
    """把任意写法的档位落到模型支持的档位上：先换别名，再取模型支持的最近一档（同样近取高的）。"""
    if not effort:
        return None
    e = EFFORT_ALIAS.get(str(effort).lower(), str(effort).lower())
    if e not in EFFORT_ORDER:
        raise ValueError(f"不认识的思考档位：{effort}（可用：none / minimal / low / medium / high / xhigh / max）")
    if not supported or e in supported:
        return e
    i = EFFORT_ORDER.index(e)
    cands = [s for s in supported if s in EFFORT_ORDER]
    return min(cands, key=lambda s: (abs(EFFORT_ORDER.index(s) - i), -EFFORT_ORDER.index(s))) if cands else e


def resolve_model(cfg, name):
    """模型别名 → (完整 id, 别名信息)。不是别名就当完整 id。"""
    models = cfg.get("models") or {}
    if name in models:
        return models[name].get("id") or name, models[name], name
    for alias, m in models.items():
        if m.get("id") == name:
            return name, m, alias
    return name, {}, name


def now():
    return time.time()


def in_hours(prof, t=None):
    """档案的可用时段（hours: "09:30-01:00"，UTC，可跨午夜）。没设 = 随时可用。"""
    h = (prof or {}).get("hours")
    if not h:
        return True
    try:
        a, b = [int(x.split(":")[0]) * 60 + int(x.split(":")[1]) for x in str(h).split("-")]
    except (ValueError, IndexError):
        return True
    g = time.gmtime(t or time.time())
    cur = g.tm_hour * 60 + g.tm_min
    return a <= cur < b if a < b else (cur >= a or cur < b)


def stamp(t=None):
    return time.strftime("%H:%M:%S", time.localtime(t or now()))


# ───────── 故障告警（~/.crew/alerts.json）：后台服务的工人线程、巡检线程写；命令行只读和手动解除
ALERTS = os.path.join(HOME, "alerts.json")
URL_RE = re.compile(r"https?://([A-Za-z0-9.-]+)(:\d+)?|\b(\d{1,3}(?:\.\d{1,3}){3})(:\d+| port \d+)?")


def _alerts_locked(fn):
    ensure_dirs()
    with open(os.path.join(HOME, "alerts.lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        d = read_json(ALERTS) or {}
        r = fn(d)
        write_json(ALERTS, d)
        return r


def scan_alerts(text, cfg, code=1):
    """在一段工具输出里找故障特征。返回 [(key, label, host, 那一行)]，同一 key 只报一次。
    命令成功退出（code 0）时只认行首的 [CREW-ALERT]：cat 一份源码、grep 日志里的旧报错不算故障。"""
    found = {}
    for label, pat in cfg.get("alert_patterns") or []:
        if code == 0 and "CREW-ALERT" not in pat:
            continue
        for m in re.finditer(pat, text or "", re.I | re.M):
            a, b = text.rfind("\n", 0, m.start()) + 1, text.find("\n", m.end())
            line = text[a:b if b >= 0 else len(text)].strip()[:300]
            u = URL_RE.search(text[max(0, m.start() - 300):m.end() + 300])
            host = ((u.group(1) or u.group(3)) + (u.group(2) or (u.group(4) or "").replace(" port ", ":"))) if u else ""
            tag = re.match(r"\[CREW-ALERT(?::([\w.-]+))?\]", line) if "CREW-ALERT" in pat else None
            if tag:
                # [CREW-ALERT:名字] 说明：同一个源的各种错误（502、超时、连不上）归成一条告警
                label = re.split(r"[。；\n]", line[tag.end():].strip())[0][:80] or label
                if tag.group(1):
                    host = tag.group(1)
            key = host or label
            if key not in found:
                found[key] = (key, label, host, line)
            break
    return list(found.values())


def watched(host, label, cfg):
    return any(w in (host or label or "") for w in cfg.get("alert_watch") or [])


def raise_alert(key, label, host, sample, source, cfg, task=None):
    """开（或累加）一条告警。task 是遇到它的任务（带 owner / origin，用来决定叫醒哪个对话）。返回告警。"""
    # 归并：属于某个巡检管的源（地址、说明里带它的 match）的，一律记在那个巡检的 key 下——同一次故障只有一条告警
    for p in cfg.get("probes") or []:
        if p.get("key") and any(m and (m in key or m in (host or "") or m in sample or m in label) for m in p.get("match") or []):
            key, host, label = p["key"], p["key"], f"{p.get('name') or p['key']}不可用"
            break

    def go(d):
        a = d.get(key)
        if not a or a.get("status") != "open":
            a = {"key": key, "label": label, "host": host, "first": now(), "count": 0, "tasks": [], "owners": [], "sessions": [], "source": source}
        a.update(status="open", last=now(), sample=sample[:300].replace("\x1b", ""), resolved=None)
        a["count"] += 1
        if source != a["source"] and source not in a["source"]:
            a["source"] = a["source"] + "、" + source
        if task:
            for k, v in (("tasks", task["id"]), ("owners", task.get("owner")), ("sessions", (task.get("origin") or {}).get("session"))):
                if v and v not in a[k]:
                    a[k] = (a[k] + [v])[-30:]
        a["title"] = f"{label}（{host}）" if host and host not in label else label
        # 要不要叫醒 Claude：数据接口 / 本机服务、工人或巡检报的、反复出现、波及多个任务
        a["important"] = bool(watched(host, label, cfg) or "工人报告" in a["source"] or "巡检" in a["source"]
                              or a["count"] >= 3 or len(a["tasks"]) >= 2)
        d[key] = a
        return dict(a)
    return _alerts_locked(go)


def resolve_alerts(match, why):
    """解除命中 match（地址片段列表，或单个 key）的告警。返回解除了的 key。"""
    ms = [match] if isinstance(match, str) else list(match)

    def go(d):
        done = []
        for k, a in d.items():
            if a.get("status") == "open" and any(m and (m == k or m in (a.get("host") or "") or m in k or m in (a.get("sample") or "")) for m in ms):
                a.update(status="resolved", resolved=now(), resolved_why=why)
                done.append(k)
        # 只留最近 3 天的
        for k in [k for k, a in d.items() if a.get("status") != "open" and now() - (a.get("resolved") or a.get("last") or 0) > 3 * 86400]:
            d.pop(k)
        return done
    return _alerts_locked(go)


def delete_alerts(keys):
    """删掉告警（不管开着还是已解除）：对话确认是误报、或已解决不想再看到时用。返回删了的 key。
    同一个源之后再出问题会重新开一条。"""
    ks = set(keys)

    def go(d):
        done = [k for k in d if k in ks]
        for k in done:
            d.pop(k)
        return done
    return _alerts_locked(go)


def list_alerts(within=86400):
    """开着的告警 + within 秒内解除的。开着但 2 小时没再出现、又没有巡检管的，算“安静了”（不再算开着）。"""
    d = read_json(ALERTS) or {}
    out = []
    for a in d.values():
        a = dict(a)
        if a.get("status") == "open" and now() - (a.get("last") or 0) > 7200 and "巡检" not in a.get("source", ""):
            a["status"] = "quiet"
        if a["status"] == "open" or now() - (a.get("resolved") or a.get("last") or 0) < within:
            out.append(a)
    return sorted(out, key=lambda a: -(a.get("last") or 0))


def alert_digest_key(text):
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]


def ensure_dirs():
    for d in (HOME, TASKS, CTL):
        os.makedirs(d, exist_ok=True)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{random.randint(0, 1 << 30)}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def config():
    path = os.path.join(HOME, "config.json")
    if not os.path.exists(path):
        ensure_dirs()
        write_json(path, DEFAULT_CONFIG)
    user = read_json(path)
    if not isinstance(user, dict):
        # 文件在、但读不出来（正在编辑、写坏了）：不覆盖用户的配置，用上次读成功的
        print(f"crew：{path} 不是合法 JSON，暂用上次读到的配置", file=sys.stderr)
        user = _last_cfg.get("user") or {}
    else:
        _last_cfg["user"] = user
    return deep_merge(DEFAULT_CONFIG, user)


_last_cfg = {}


def host_id():
    try:
        with open("/proc/sys/kernel/hostname") as f:
            return f.read().strip()
    except OSError:
        return socket.gethostname()


def transcript_path(sid):
    """Claude 对话的记录文件（~/.claude/projects/*/<会话 id>.jsonl）；sid 可以只写开头几位。"""
    if not sid:
        return None
    root = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")
    hits = glob.glob(os.path.join(root, "projects", "*", f"{glob.escape(sid)}*.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def session_info(sid):
    """对话的标题和第一句话。记录文件可能上百 MB：标题从末尾 4 MB 找，第一句从开头 2 MB 找。"""
    path = transcript_path(sid)
    if not path:
        return {}
    info = {"session": os.path.basename(path)[:-6]}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(2 << 20).decode("utf-8", "replace").splitlines()
            f.seek(max(0, size - (4 << 20)))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return info
    titles = {}
    for line in tail:
        if '"customTitle"' in line or '"aiTitle"' in line:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            for k in ("customTitle", "aiTitle"):
                if d.get(k):
                    titles[k] = d[k]
    info["title"] = titles.get("customTitle") or titles.get("aiTitle") or ""
    for line in head:
        if '"type":"user"' not in line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("isMeta"):
            continue
        c = (d.get("message") or {}).get("content")
        text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict) and x.get("type") == "text")
        if text.strip() and not text.lstrip().startswith("<"):
            info["first"] = clip(text.strip(), 80)
            break
    return info


def workflow_name(run_id):
    """workflow 运行编号 → 名字：运行开始时脚本存成 projects/*/<会话>/workflows/scripts/<名字>-<编号>.js。"""
    if not run_id:
        return ""
    root = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")
    for p in glob.glob(os.path.join(root, "projects", "*", "*", "workflows", "scripts", f"*-{glob.escape(run_id)}.js")):
        return os.path.basename(p)[:-len(f"-{run_id}.js")]
    for p in glob.glob(os.path.join(root, "projects", "*", "*", "workflows", f"{glob.escape(run_id)}.json")):
        return (read_json(p) or {}).get("workflowName") or ""
    return ""


def make_origin(owner, given=None):
    """派发来源：哪个 Claude 对话（会话 id、标题、第一句话）、经由哪个 workflow 或子代理、从哪个目录、哪台机器。
    插件把会话 id 和经由直接传进来；脚本从 Claude 的 Bash 里调用时，会话 id 从环境变量
    CLAUDE_CODE_SESSION_ID 取，经由从插件给 workflow 代理的 Bash 加上的 CREW_VIA 取。"""
    o = {"owner": owner, "session": os.environ.get("CLAUDE_CODE_SESSION_ID") or "", "cwd": os.getcwd(), "host": host_id(), **(given or {})}
    if not o.get("via") and os.environ.get("CREW_VIA"):
        try:
            o["via"] = json.loads(os.environ["CREW_VIA"])
        except json.JSONDecodeError:
            pass
    via = o.get("via")
    if isinstance(via, dict) and via.get("run_id") and not via.get("workflow"):
        via["workflow"] = workflow_name(via["run_id"])
    if o["session"]:
        o.update({k: v for k, v in session_info(o["session"]).items() if v and not o.get(k)})
    return o


def via_label(via):
    """经由：workflow 名字（第几个代理「标签」），或子代理「标签」。"""
    if not isinstance(via, dict) or not via:
        return ""
    who = f"「{via['label']}」" if via.get("label") else ""
    if via.get("kind") == "workflow":
        n = f"第 {via['agent_index']} 个代理" if via.get("agent_index") else "代理"
        return f"workflow {via.get('workflow') or via.get('run_id') or '?'}（{n}{who}）"
    return f"子代理{who}" + (f"（{via['agent_type']}）" if via.get("agent_type") else "")


_TOPICS = {}


def session_topic(sid):
    """对话主题：标题，没有就用第一句话。查到的结果缓存在 topics.json 里 60 秒，面板每 3 秒刷新也不会反复读上百 MB 的记录。"""
    if not sid:
        return ""
    path = os.path.join(HOME, "topics.json")
    if not _TOPICS:
        _TOPICS.update(read_json(path, {}) or {})
    hit = _TOPICS.get(sid)
    if hit and now() - hit.get("t", 0) < 60:
        return hit["topic"]
    info = session_info(sid)
    topic = info.get("title") or info.get("first") or (hit or {}).get("topic") or ""
    _TOPICS[sid] = {"topic": topic, "t": now()}
    try:
        write_json(path, _TOPICS)
    except OSError:
        pass
    return topic


def origin_label(t, width=None):
    """一行说清派发来源：对话主题「…」› workflow / 子代理 · 脚本；找不到主题才写编号；不是从对话派的写派发者（你 / cli / 脚本名）。"""
    o = t.get("origin") or {}
    owner = str(t.get("owner") or o.get("owner") or "—")
    sid = o.get("session") or (owner[7:] if owner.startswith("claude:") else "")
    if sid:
        topic = session_topic(sid) or o.get("title") or o.get("first")
        s = f"「{topic}」" if topic else f"未命名对话 {sid[:8]}"
        if via_label(o.get("via")):
            s += f" › {via_label(o.get('via'))}"
        if not owner.startswith("claude:") and owner not in ("cli", "—"):
            s += f" · {owner}"            # 经脚本派的
    else:
        s = owner
    return clip(s, width) if width else s


def new_id():
    t = time.strftime("%m%d-%H%M", time.localtime())
    return f"{t}-{''.join(random.choices(string.ascii_lowercase + string.digits, k=4))}"


def task_dir(tid):
    return os.path.join(TASKS, tid)


def load_task(tid):
    return read_json(os.path.join(task_dir(tid), "task.json"))


def list_tasks():
    out = []
    try:
        names = os.listdir(TASKS)
    except FileNotFoundError:
        return out
    for n in names:
        t = load_task(n)
        if t:
            out.append(t)
    out.sort(key=lambda t: t.get("created", 0))
    return out


def owned_by(t, who):
    """任务是不是 who（claude:<会话 id 前 8 位>）这个对话派的：派发者是它，或派发来源的会话是它（脚本在它的 Bash 里派的）。"""
    if not who or t.get("owner") == who:
        return True
    sid = (t.get("origin") or {}).get("session") or ""
    return who.startswith("claude:") and bool(sid) and sid.startswith(who[7:])


def resolve_id(prefix, who=None):
    """允许用 id 的唯一前缀或末尾片段指代任务。给了 who（本对话）时，片段在本对话的任务里唯一也算。"""
    tasks = list_tasks()
    ids = [t["id"] for t in tasks]
    if prefix in ids:
        return prefix
    hits = [t for t in tasks if t["id"].startswith(prefix) or t["id"].endswith(prefix)]
    if len(hits) > 1 and who:
        hits = [t for t in hits if owned_by(t, who)] or hits
    if len(hits) == 1:
        return hits[0]["id"]
    raise SystemExit(f"找不到唯一的任务：{prefix}（匹配 {len(hits)} 个）；用完整 id")


def claim(owner, keys, release=False):
    """唤醒去重（跨进程、跨重启）：同一个对话的同一件事只唤醒一次。返回这次新认领到的 key。"""
    ensure_dirs()
    d = os.path.join(HOME, "claims")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, re.sub(r"[^\w.-]", "_", owner or "_") + ".json")
    with open(p + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        seen = {k: v for k, v in (read_json(p) or {}).items() if now() - v < 3 * 86400}
        if release:
            got = [k for k in keys if seen.pop(k, None)]
        else:
            got = [k for k in dict.fromkeys(keys) if k not in seen]
            for k in got:
                seen[k] = now()
        write_json(p, seen)
    return got


def append_event(tid, kind, **kw):
    line = json.dumps({"t": now(), "k": kind, **kw}, ensure_ascii=False)
    with open(os.path.join(task_dir(tid), "events.jsonl"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_events(tid, tail=200, max_bytes=262144):
    path = os.path.join(task_dir(tid), "events.jsonl")
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            raw = f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return []
    out = []
    for line in raw.splitlines()[-tail:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def drop(tid, kind, **payload):
    """向任务投递一条外部指令（后台服务按时间顺序消费）。"""
    if "frm" in payload:
        payload["from"] = payload.pop("frm")
    d = os.path.join(task_dir(tid), "inbox")
    os.makedirs(d, exist_ok=True)
    name = f"{time.time():.6f}-{kind}-{random.randint(0, 99999):05d}.json"
    write_json(os.path.join(d, name), {"kind": kind, "t": now(), **payload})
    return name


def daemon_info():
    info = read_json(os.path.join(HOME, "daemon.json"))
    if not info:
        return None
    info["alive"] = now() - info.get("beat", 0) < 10
    return info


def fmt_tokens(n):
    n = n or 0
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return str(n)


def fmt_secs(s):
    s = int(s or 0)
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


def elapsed(t):
    end = t.get("finished") or (now() if t.get("status") in ("running", "waiting", "paused", "queued") else t.get("updated"))
    return (end or now()) - (t.get("started") or t.get("created") or now())


def cost(t, cfg=None):
    cfg = cfg or config()
    price = (cfg["profiles"].get(t.get("profile")) or {}).get("price_per_mtok")
    if not price:
        return None
    u = t.get("usage") or {}
    cached = u.get("cached", 0)
    return ((u.get("in", 0) - cached) * price.get("in", 0) + cached * price.get("cached", price.get("in", 0))
            + u.get("out", 0) * price.get("out", 0)) / 1e6


def cwidth(ch):
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else (0 if unicodedata.combining(ch) else 1)


def swidth(s):
    return sum(cwidth(c) for c in s)


def clip(s, width, ell="…"):
    """按显示宽度截断（中文算 2 格）。"""
    s = str(s).replace("\n", " ").replace("\t", " ")
    if swidth(s) <= width:
        return s
    out, w = [], 0
    for c in s:
        cw = cwidth(c)
        if w + cw > width - 1:
            break
        out.append(c)
        w += cw
    return "".join(out) + ell


def pad(s, width):
    s = clip(s, width)
    return s + " " * (width - swidth(s))
