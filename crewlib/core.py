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

DEFAULT_CONFIG = {'max_parallel': 4,
 'default_profile': 'gpt',
 'force_effort': None,
 'models': {},
 'defaults': {'context_tokens': 200000},
 'profiles': {'gpt': {'backend': 'codex',
                      'codex_bin': 'codex',
                      'model': None,
                      'effort': None,
                      'max_parallel': 4,
                      'parallel_group': 'gpt',
                      'codex_subagents': False,
                      'codex_skill_roots': []},
              'gpt-chat': {'backend': 'chat',
                           'base_url': 'https://api.openai.com/v1',
                           'api_key_env': 'OPENAI_API_KEY',
                           'env_file': None,
                           'model': None,
                           'effort': None,
                           'max_tokens': 16384,
                           'max_tokens_field': 'max_completion_tokens',
                           'max_parallel': 4,
                           'parallel_group': 'gpt',
                           'request_timeout_s': 900,
                           'retries': 6,
                           'vision': True},
              'claude': {'backend': 'claude',
                         'claude_bin': 'claude',
                         'model': None,
                         'effort': None,
                         'max_parallel': 2,
                         'quota_stop': 0.9,
                         'retries': 4,
                         'vision': True,
                         'explicit_only': True},
              'mock': {'backend': 'mock'}},
 'tool_output_chars': 8000,
 'peer_cache_margin_s': 180,
 'alert_patterns': [['脚本报告故障', '^\\[CREW-ALERT(?::[\\w.-]+)?\\][^\\n]*'],
                    ['上游服务 5xx',
                     'UPSTREAM_5XX|upstream_status\\"?\\s*:\\s*5\\d\\d|HTTP Error 5\\d\\d|\\b5\\d\\d (Bad '
                     'Gateway|Service Unavailable|Gateway Time-?out|Internal Server Error)|\\b5\\d\\d from '
                     'https?://'],
                    ['连不上服务',
                     'Connection refused|ECONNREFUSED|服务连不上|Failed to establish a new connection|Name or '
                     'service not known|No route to host|Temporary failure in name resolution'],
                    ['密钥或额度',
                     'invalid[_ ]api[_ ]key|insufficient_quota|quota '
                     'exceeded|ModelNotOpen|AuthenticationError']],
 'alert_watch': ['127.0.0.1', 'localhost'],
 'mcp_servers': {},
 'mcp_env_file': None,
 'mcp_timeout_s': 180,
 'probes': [],
 'bash_timeout_s': 600,
 'worker_env': {},
 'worker_env_note': '环境：只写任务允许的位置。python3 / pip 使用安装时选择的 Python，可用 CREW_PYTHON '
                    '覆盖；缺少的依赖按任务需要安装。网络请求应设置超时。完成后调用 submit_result。'}

# 思考档位从低到高；Claude Code 的 low / medium / high / xhigh / max 照收。
# 写哪个都行，落到模型支持的最近一档（同样近取高的）
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




def model_label(t, cfg):
    """任务实际用的模型和后端。
    旧任务记的是别名（gpt·high·codex），按当时的别名换成完整名。"""
    used = str(t.get("model_used") or "")
    if used:
        parts = [x.strip() for x in used.replace("·", " · ").split("·") if x.strip()]
        if parts:
            parts[0] = resolve_model(cfg, parts[0])[0]
            parts = [("Codex" if x == "codex" else "Claude" if x == "claude" else x) for x in parts]
        return " · ".join(parts)
    if t.get("status") != "queued":
        return ""                       # 没开过工（或很早的任务没记）：不猜
    prof = (cfg.get("profiles") or {}).get(profile_name(t.get("profile"), cfg)) or {}
    mid = resolve_model(cfg, t.get("model") or prof.get("model") or "")[0]
    eff = t.get("effort") or prof.get("effort")
    return " · ".join(x for x in (mid, eff) if x) + "（待开工）" if mid else ""


def now():
    return time.time()


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
    return socket.gethostname()


def transcript_path(sid):
    """Claude 对话的记录文件（~/.claude/projects/*/<会话 id>.jsonl）；sid 可以只写开头几位。"""
    if not sid:
        return None
    root = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")
    hits = glob.glob(os.path.join(root, "projects", "*", f"{glob.escape(sid)}*.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def profile_name(name, cfg=None):
    """任务用的档案名：没写、或写的档案不存在，都用 default_profile。"""
    cfg = cfg or config()
    name = name or cfg.get("default_profile") or "gpt"
    return name if name in cfg["profiles"] else (cfg.get("default_profile") or "gpt")


def _iso_ts(v):
    """ISO 时间串（2026-11-27T05:30:00.123Z）→ epoch 秒；不认得返回 0。"""
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0


def session_activity(sid):
    """对话最后一次模型请求的时间、提示缓存 TTL、上下文大小（只看主线，不看子代理）。
    提示缓存从最后一次请求起算 TTL（Claude Code 记在 promptCacheRequested：1h / 5m）；过了 TTL 缓存就没了，
    再发消息过去它要按写入价重写整个上下文。Claude Code 右下角的 "new task? /clear to save N tokens"
    是闲置 ≥ 75 分钟（CLAUDE_CODE_IDLE_THRESHOLD_MINUTES）且上下文 ≥ 10 万 token（CLAUDE_CODE_IDLE_TOKEN_THRESHOLD）
    时出的提示，比缓存过期晚，不能拿它判断。"""
    path = transcript_path(sid)
    info = {"transcript": path or "", "last_request": 0, "ttl_s": 3600, "context_tokens": 0}
    if not path:
        return info
    try:
        size = os.path.getsize(path)
        for span in (1 << 20, 16 << 20):
            with open(path, "rb") as f:
                f.seek(max(0, size - span))
                lines = f.read().decode("utf-8", "replace").splitlines()
            for line in reversed(lines):
                if '"type":"assistant"' not in line or '"isSidechain":true' in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "assistant" or d.get("isSidechain"):
                    continue
                info["last_request"] = _iso_ts(d.get("timestamp"))
                ttl = str(d.get("promptCacheRequested") or "1h")
                info["ttl_s"] = 300 if ttl.endswith("m") and ttl[:-1] == "5" else (int(ttl[:-1]) * 60 if ttl.endswith("m") and ttl[:-1].isdigit() else 3600)
                u = (d.get("message") or {}).get("usage") or {}
                info["context_tokens"] = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
                return info
            if span >= size:
                break
    except OSError:
        pass
    return info


def cache_state(s, act=None):
    """对方对话的提示缓存还在不在：{warm, idle_s, left_s, ...activity}。忙（正在跑回合）算在；从没请求过也算在（没有可丢的缓存）。"""
    act = act or session_activity(s.get("sessionId") or s.get("session") or "")
    last = act.get("last_request") or 0
    idle = max(0, now() - last) if last else 0
    left = act["ttl_s"] - idle if last else act["ttl_s"]
    margin = int(config().get("peer_cache_margin_s") or 180)
    warm = s.get("status") == "busy" or not last or left > margin
    return dict(act, warm=bool(warm), idle_s=int(idle), left_s=int(left))


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
    """一行说清派发来源：对话主题「…」› workflow / 子代理 · 脚本；找不到主题才写编号；不是从对话派的写派发者（你 / cli / script）。"""
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
