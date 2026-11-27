"""工人之间协作。

队伍 = 同一个 Claude 对话派出的任务（含它跑的工作流里的代理）；不是从 Claude 对话派的，按 owner 分。
三个工具（chat / Codex / Claude 三种后端都有）：
- team_list：队友清单（同一批、同一工作流的标出来）——状态、成果文件、汇报首段、正在做什么；加上队伍公告板最近的帖子。
  scope=all 看本机近 3 天所有任务（跨对话，比如别的项目里已经查过的东西）。
- team_read：读任意一个任务的完整成果（任务原文、完整汇报、全部文件、自查、未解决、data），没交成果的给进展和它写过的文件。
- team_post：往队伍公告板发一条（发现、已落盘的中间结果、提醒队友的坑）；所有队友 team_list 时都能看到。
公告板：~/.crew/boards/<队伍>.jsonl。读别人的文件随便读，不要改别人的文件。
"""
import json
import os
import re

from . import core

BOARDS = os.path.join(core.HOME, "boards")
ACTIVE = ("queued", "running", "paused", "waiting")


def team_key(t):
    o = t.get("origin") or {}
    return o.get("session") or str(t.get("owner") or "cli")


def board_path(key):
    return os.path.join(BOARDS, re.sub(r"[^\w.-]", "_", key)[:80] + ".jsonl")


def first_para(s, n=400):
    s = str(s or "").strip()
    p = s.split("\n\n")[0].strip()
    return p if len(p) <= n else p[:n].rstrip() + "…（全文用 team_read）"


def result_of(tid):
    return core.read_json(os.path.join(core.task_dir(tid), "result.json"))


def first_line(s, n=160):
    s = str(s or "").strip().split("\n")[0].strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def row(t, me, res=None, close=False):
    tid = t["id"]
    res = res if res is not None else result_of(tid)
    tags = []
    if tid == me.get("id"):
        tags.append("你自己")
    if t.get("batch") and t.get("batch") == me.get("batch"):
        tags.append("同一批")
    wf, mwf = (t.get("wf") or {}).get("run"), (me.get("wf") or {}).get("run")
    if wf and wf == mwf:
        tags.append("同一工作流")
    st = core.STATUS_ZH.get(t.get("status"), t.get("status"))
    head = f"- {tid}「{t.get('title')}」{st}" + (f"（{'、'.join(tags)}）" if tags else "") + f" · 目录 {t.get('cwd')}"
    lines = [head]
    if res:
        files = res.get("files") or []
        if close:                                   # 同一批 / 同一工作流：多给一点
            lines.append(f"  汇报首段：{first_para(res.get('summary'))}")
            if files:
                lines.append("  文件：" + "；".join(files))
        else:
            lines.append(f"  结论：{first_line(res.get('summary'))}" + (f"（文件 {len(files)} 个）" if files else ""))
    elif t.get("status") in ACTIVE and t.get("activity"):
        lines.append(f"  正在：{t.get('activity')}")
    return "\n".join(lines)


def team_list(t, scope="team", status=None, query=None):
    cut = core.now() - 3 * 86400
    key = team_key(t)
    tasks = [x for x in core.list_tasks() if (x.get("created") or 0) >= cut]
    if scope != "all":
        tasks = [x for x in tasks if team_key(x) == key]
    if status == "done":
        tasks = [x for x in tasks if x.get("status") in ("review", "accepted")]
    elif status == "active":
        tasks = [x for x in tasks if x.get("status") in ACTIVE]
    if query:
        ws = [w.lower() for w in str(query).split() if w]
        def hay(x):
            r = result_of(x["id"]) or {}
            return " ".join([str(x.get("title") or ""), str(x.get("prompt") or "")[:2000], str(r.get("summary") or ""), " ".join(r.get("files") or [])]).lower()
        tasks = [x for x in tasks if all(w in hay(x) for w in ws)]
    me_b, me_w = t.get("batch"), (t.get("wf") or {}).get("run")
    close = lambda x: bool((me_b and x.get("batch") == me_b) or (me_w and (x.get("wf") or {}).get("run") == me_w))
    tasks.sort(key=lambda x: (not (me_b and x.get("batch") == me_b), not (me_w and (x.get("wf") or {}).get("run") == me_w), -(x.get("created") or 0)))
    out = [f"{'本机近 3 天所有任务' if scope == 'all' else '队友（同一个 Claude 对话派的任务）'}：{len(tasks)} 个。读完整成果用 team_read(id)；他们列的文件可以直接读，不要改。"]
    out += [row(x, t, close=close(x)) for x in tasks[:80]]
    if len(tasks) > 80:
        out.append(f"（另 {len(tasks) - 80} 个较早的没列：用 query 按关键词找，或按 id 用 team_read 读）")
    posts = read_board(key)
    if posts:
        out += ["", f"队伍公告板（最近 {len(posts)} 条，旧的在前）："]
        out += [f"- [{time_s(p.get('t'))} {p.get('task')}「{p.get('title')}」] {p.get('text')}" for p in posts]
    return "\n".join(out)


def time_s(ts):
    import time
    return time.strftime("%m-%d %H:%M", time.gmtime(ts or 0)) + "Z"


def team_read(tid):
    from . import cli
    try:
        tid = core.resolve_id(tid)
        t = core.load_task(tid)
    except (SystemExit, Exception) as e:      # noqa: BLE001
        return f"找不到任务 {tid}：{e}", 1
    res = result_of(tid)
    parts = [f"── 任务 {tid}「{t.get('title')}」（{core.STATUS_ZH.get(t.get('status'), t.get('status'))}，目录 {t.get('cwd')}）",
             "── 任务原文", str(t.get("prompt") or "")]
    if t.get("accept"):
        parts += ["── 验收标准", t["accept"]]
    parts += ["── 成果", cli.digest_text(t, res)]
    if not res:
        if t.get("activity"):
            parts.append(f"正在：{t['activity']}")
        says = [e.get("text", "") for e in core.read_events(tid, tail=400) if e.get("k") == "say" and e.get("text", "").strip()][-3:]
        if says:
            parts += ["── 它最近说的", *says]
    return "\n".join(parts), 0


def read_board(key, n=30):
    p = board_path(key)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out[-n:]


def team_post(t, text):
    text = str(text or "").strip()
    if not text:
        return "没写内容。", 1
    os.makedirs(BOARDS, exist_ok=True)
    with open(board_path(team_key(t)), "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": core.now(), "task": t["id"], "title": t.get("title"), "text": text}, ensure_ascii=False) + "\n")
    return "已发到队伍公告板，队友 team_list 时能看到。", 0


TOOLS = [
    {"name": "team_list", "description": "看队友：同一个 Claude 对话派的其他任务（同一批、同一工作流的排前面）——状态、成果文件、汇报首段、正在做什么，以及队伍公告板上的帖子。开工前先看一眼，别人已经查到、已经落盘的东西直接用，不要重复做。scope=all 看本机近 3 天所有任务（跨对话）。",
     "parameters": {"type": "object", "properties": {
         "scope": {"type": "string", "enum": ["team", "all"], "description": "team = 本队（默认）；all = 本机近 3 天所有任务"},
         "status": {"type": "string", "enum": ["any", "done", "active"], "description": "done = 只看已交成果的；active = 只看还在做的；默认全部"},
         "query": {"type": "string", "description": "关键词（空格分隔、都要命中）：在标题、任务原文、汇报、文件路径里找，如 \"HBM 份额\""}}}},
    {"name": "team_read", "description": "读任意一个任务的完整成果：任务原文、完整汇报、全部文件（带大小）、自查、未解决、data；还没交的给进展和它最近说的。它列的文件可以直接读来用（不要改别人的文件），引用时写明出自哪个任务。",
     "parameters": {"type": "object", "properties": {"id": {"type": "string", "description": "任务 id（team_list 里的）"}}, "required": ["id"]}},
    {"name": "team_post", "description": "往队伍公告板发一条，所有队友都能看到：对别人有用的发现（带出处）、已经落盘可以复用的中间结果（写路径）、踩过的坑（某数据源坏了、某口径有陷阱）。只发对队友有用的，不发进度流水账。",
     "parameters": {"type": "object", "properties": {"text": {"type": "string", "description": "要说的事，写清楚出处或文件路径"}}, "required": ["text"]}},
]
NAMES = [x["name"] for x in TOOLS]
