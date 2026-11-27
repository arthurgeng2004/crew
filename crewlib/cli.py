"""crew 命令行。人和 Claude 插件都用它；带 --json 的输出给程序读。"""
import argparse
import contextlib
import json
import re
import os
import subprocess
import sys
import time

from . import core

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def out(obj, as_json):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False))
    else:
        print(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=1))


# ───────── 后台服务
def cmd_effort(a):
    """看 / 改各档案的默认思考档位（写进 ~/.crew/config.json）。任务、工作流里写的 effort 优先于默认。"""
    cfg = core.config()
    path = os.path.join(core.HOME, "config.json")
    if a.profile and a.level:
        if a.profile not in cfg["profiles"]:
            sys.exit(f"没有档案 {a.profile}；现有：{', '.join(cfg['profiles'])}")
        lv = core.fit_effort(a.level)                     # 校验写法
        user = core.read_json(path) or {}
        user.setdefault("profiles", {}).setdefault(a.profile, {})["effort"] = lv
        core.write_json(path, user)
        cfg = core.config()
        print(f"{a.profile} 的默认思考档位改成 {lv}（新派的任务生效；已在跑的任务不变）")
    if cfg.get("force_effort"):
        print(f"注意：全局 force_effort = {cfg['force_effort']}，压过一切；要按档案 / 任务调，把它设成 null")
    for name, p in cfg["profiles"].items():
        if p.get("backend") == "mock":
            continue
        mid, info, alias = core.resolve_model(cfg, p.get("model"))
        sup = info.get("efforts") or p.get("efforts") or []
        eff = core.fit_effort(p.get("effort"), sup) if p.get("effort") else "模型默认"
        print(f"  {name:<9} {alias}（{mid}）  默认 {eff}   可选 {' / '.join(sup) or '-'}")
    print("按任务调：派活写 effort（low / high / max …），工作流 agent(…, { effort })，命令行 crew run … -e high")


SESS_DIR = os.path.join(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"), "sessions")
ACTIVE = ("queued", "running", "paused", "waiting")


def live_sessions():
    """本机正开着的 Claude 对话（~/.claude/sessions/<pid>.json，进程还在、且不是 pid 被复用）。"""
    import glob
    out = []
    for f in glob.glob(os.path.join(SESS_DIR, "*.json")):
        s = core.read_json(f) or {}
        pid = int(s.get("pid") or 0)
        if not pid or not s.get("sessionId"):
            continue
        from .platform import session_alive
        if not session_alive(pid, s.get("procStart"), s.get("startedAt")):
            continue
        out.append(s)
    return out


def cmd_peers(a):
    """本机正开着的 Claude 对话：名字、会话 id、忙闲、主题，以及近 3 天用没用 crew（派过的任务 / 工作流、还在跑的）。
    插件的 message 工具靠它找收件人；--me 标出自己。"""
    from . import wf
    cut = core.now() - 3 * 86400
    tasks = [t for t in core.list_tasks() if (t.get("created") or 0) >= cut]
    wfs = [w for w in (wf.load(i) for i in wf.list_ids()) if w and (w.get("created") or 0) >= cut]
    rows = []
    workers = {t.get("claude_session") for t in core.list_tasks() if t.get("claude_session")}   # Claude 工人的会话不算对话
    for s in live_sessions():
        sid = s["sessionId"]
        if sid in workers:
            continue
        own = f"claude:{sid[:8]}"
        mine = lambda x: x.get("owner") == own or (x.get("origin") or {}).get("session") == sid
        ts = [t for t in tasks if mine(t) and not t.get("wf")]
        ws = [w for w in wfs if mine(w)]
        cs = core.cache_state(s)
        rows.append({
            "name": s.get("name") or "", "session": sid, "status": s.get("status") or "", "kind": s.get("kind") or "",
            "topic": core.session_topic(sid), "me": sid == (a.me or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""),
            "cwd": s.get("cwd") or "", "transcript": cs["transcript"], "cache_warm": cs["warm"], "idle_s": cs["idle_s"],
            "cache_left_s": cs["left_s"], "cache_ttl_s": cs["ttl_s"], "context_tokens": cs["context_tokens"],
            "crew_tasks": len(ts), "crew_workflows": len(ws),
            "crew_active": sum(t.get("status") in ACTIVE for t in ts) + sum(w.get("status") not in wf.DONE for w in ws),
        })
    rows.sort(key=lambda r: (not r["me"], -(r["crew_tasks"] + r["crew_workflows"]), r["name"]))
    if a.json:
        out(rows, True)
        return
    for r in rows:
        use = f"crew：近 3 天任务 {r['crew_tasks']}、工作流 {r['crew_workflows']}，在跑 {r['crew_active']}" if r["crew_tasks"] + r["crew_workflows"] else "没用 crew"
        cache = "缓存在" if r["cache_warm"] else f"缓存已过期（闲置 {r['idle_s'] // 60} 分钟）"
        print(f"{'*' if r['me'] else ' '} {core.pad(r['name'], 24)}{r['session'][:8]}  {core.pad(r['status'], 6)}{r['topic'][:40]}  〔{use}；{cache}〕")


def _peer_pick(to):
    """按名字 / 会话 id 前缀 / 标题里的词找一个开着的对话（和插件 message 工具同一套）。"""
    ss = live_sessions()
    exact = [s for s in ss if s.get("name") == to or s["sessionId"].startswith(to)]
    hits = exact or [s for s in ss if to and to in core.session_topic(s["sessionId"])]
    if len(hits) != 1:
        names = "、".join(f"{s.get('name')}（{core.session_topic(s['sessionId'])[:20]}）" for s in (hits or ss))
        raise SystemExit(f"「{to}」{'对上了多个对话' if hits else '没找到'}：{names}")
    return hits[0]


def _brief(x, n):
    x = " ".join(str(x).split())
    return x if len(x) <= n else x[:n] + "…"


def cmd_peer_log(a):
    """读另一个对话的上下文（不打扰它、不花它的缓存）：最近 n 条主线消息（用户 / Claude 的话、工具调用摘要），
    加工作目录、记录文件路径和缓存状态。对方缓存过期时 message 工具不发，就用这个了解它在做什么，再直接读它的文件。"""
    s = _peer_pick(a.to)
    sid = s["sessionId"]
    cs = core.cache_state(s)
    path = cs["transcript"]
    print(f"「{core.session_topic(sid)}」 {s.get('name')} {sid}")
    print(f"工作目录：{s.get('cwd') or '?'}　记录：{path or '（没找到）'}")
    last = time.strftime('%m-%d %H:%M', time.gmtime(cs['last_request'])) + ' UTC' if cs['last_request'] else '无'
    print(f"状态：{s.get('status') or '?'}　最后一次请求：{last}（闲置 {cs['idle_s'] // 60} 分钟，TTL {cs['ttl_s'] // 60} 分钟）　"
          f"缓存：{'在' if cs['warm'] else '已过期'}　上下文约 {cs['context_tokens']:,} token")
    if not path:
        return
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - (a.bytes << 20)))
        lines = f.read().decode("utf-8", "replace").splitlines()
    out_rows = []
    for line in lines:
        if '"isSidechain":true' in line or ('"type":"user"' not in line and '"type":"assistant"' not in line):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        m = d.get("message") or {}
        role, c = m.get("role"), m.get("content")
        ts = time.strftime('%H:%M', time.gmtime(core._iso_ts(d.get("timestamp")))) if d.get("timestamp") else "--:--"
        parts = []
        for b in ([{"type": "text", "text": c}] if isinstance(c, str) else (c or [])):
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and b.get("text", "").strip():
                if role == "user" and b["text"].lstrip().startswith("<"):   # 系统注入（system-reminder、命令回显等）
                    continue
                parts.append(_brief(b["text"], a.chars))
            elif t == "tool_use":
                inp = b.get("input") or {}
                arg = inp.get("description") or inp.get("command") or inp.get("file_path") or inp.get("prompt") or json.dumps(inp, ensure_ascii=False)
                parts.append(f"[{b.get('name')}] {_brief(arg, 160)}")
            elif t == "tool_result" and a.results:
                rc = b.get("content")
                rc = " ".join(x.get("text", "") for x in rc if isinstance(x, dict)) if isinstance(rc, list) else rc
                parts.append("  ↳ " + _brief(rc or "", 200))
        if parts:
            out_rows.append(f"{ts} {'用户' if role == 'user' and not any(p.startswith('  ↳') for p in parts) else ('Claude' if role == 'assistant' else '结果')}：" + "\n      ".join(parts))
    print(f"── 最近 {min(a.n, len(out_rows))} 条（共扫到 {len(out_rows)} 条；更多用 -n / --bytes，工具结果加 --results）")
    for r in out_rows[-a.n:]:
        print(r)


def cmd_msgs(a):
    """跨对话消息记录（~/.crew/messages.jsonl）：--add 从标准输入追加一条（插件发完消息记账用）；否则列最近 n 条。"""
    p = os.path.join(core.HOME, "messages.jsonl")
    if a.add:
        rec = json.loads(sys.stdin.read() or "{}")
        rec.setdefault("t", core.now())
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out({"ok": True}, a.json)
        return
    try:
        lines = open(p, encoding="utf-8").read().splitlines()[-a.n:]
    except FileNotFoundError:
        lines = []
    recs = [json.loads(x) for x in lines if x.strip()]
    if a.json:
        out(recs, True)
        return
    for r in recs:
        ok = "已送达" if r.get("delivered") else f"未送达（{r.get('reason', '')}）"
        print(f"{time.strftime('%m-%d %H:%M', time.gmtime(r['t']))} UTC 「{r.get('from_topic', '')}」→「{r.get('to_topic', '')}」{ok}：{r.get('text', '')[:120]}")


def cmd_models(a):
    """列出一个档案的接口上能用的模型，并按档案设置实际发一条请求：接 GPT 等新接口时先跑这个。"""
    import urllib.error
    import urllib.request
    from .worker import Worker, opener
    cfg = core.config()
    if a.profile not in cfg["profiles"]:
        sys.exit(f"没有档案 {a.profile}；现有：{', '.join(cfg['profiles'])}")
    prof = cfg["profiles"][a.profile]
    if prof.get("backend") == "mock":
        sys.exit("mock 档案不连接口")
    if prof.get("backend") in ("codex", "claude"):
        print("此档案使用本机 CLI 的默认模型；使用对应 CLI 查看模型，任务可显式传 --model。")
        return
    w = Worker.__new__(Worker)
    w.prof, w.cfg, w.task, w.tid = prof, cfg, {}, ""
    w.d = type("D", (), {"tasks": {}})()
    key = w.api_key()
    if not key:
        sys.exit(f"没有 key：在 {prof.get('env_file')} 里写一行 {prof.get('api_key_env')}=...（或设环境变量 {prof.get('api_key_env')}）")
    base = prof["base_url"].rstrip("/")
    hdr = {"Authorization": f"Bearer {key}", **prof.get("headers", {})}
    try:
        j = json.loads(opener(prof).open(urllib.request.Request(base + "/models", headers=hdr), timeout=30).read())
        ids = sorted(m.get("id", "") for m in j.get("data") or [])
        keep = [i for i in ids if not a.filter or a.filter in i]
        print(f"{base}/models：{len(ids)} 个" + (f"，含“{a.filter}”的 {len(keep)} 个" if a.filter else ""))
        for i in keep[:200]:
            print("  " + i)
    except urllib.error.HTTPError as e:
        print(f"列模型失败：HTTP {e.code} {e.read().decode('utf-8', 'replace')[:300]}")
    except Exception as e:  # noqa: BLE001
        print(f"列模型失败：{type(e).__name__}: {e}")
    mid, eff, alias = w.model_params()
    body = w.request_body.__func__(type("W", (), {"model_params": lambda s_: (mid, eff, alias), "prof": prof, "update": lambda *x, **k: None,
                                                  "ev": lambda *x, **k: None})(),
                                   [{"role": "user", "content": "17*23 等于多少？只回答数字"}], [])
    body.pop("tools", None)
    body.pop("tool_choice", None)
    body[prof.get("max_tokens_field") or "max_tokens"] = 2000
    print(f"\n试一条：模型 {mid}（别名 {alias}）· 思考档位 {eff or '默认'} · 参数 {', '.join(k for k in body if k not in ('messages',))}")
    t0 = time.time()
    try:
        r = json.loads(opener(prof).open(urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(),
                                                                     headers={"Content-Type": "application/json", **hdr}), timeout=120).read())
        m = r["choices"][0]["message"]
        u = r.get("usage") or {}
        print(f"  ✓ {time.time() - t0:.1f}s  回答：{(m.get('content') or '').strip()[:60]}  · 入 {u.get('prompt_tokens')} 出 {u.get('completion_tokens')}"
              + (f"（推理 {(u.get('completion_tokens_details') or {}).get('reasoning_tokens')}）" if u.get("completion_tokens_details") else ""))
        print(f"  这个档案能用：派活时写 profile: \"{a.profile}\"（工作流 agent 选项同名）")
    except urllib.error.HTTPError as e:
        print(f"  ✗ HTTP {e.code}：{e.read().decode('utf-8', 'replace')[:400]}")
        print("  模型 id 不对就改 ~/.crew/config.json 的 models（上面列出的是账号上能用的）")
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {type(e).__name__}: {e}")


def cmd_up(a):
    stopped = os.path.join(core.HOME, "stopped")
    if getattr(a, "watch", False):
        # 守护调用：用户有意 crew down 了就不拉；在运行就什么都不说
        info = core.daemon_info()
        if os.path.exists(stopped) or (info and info["alive"]):
            return
    try:
        os.remove(stopped)
    except OSError:
        pass
    info = core.daemon_info()
    if info and info["alive"]:
        out({"ok": True, "already": True, **info}, a.json) if a.json else print(f"后台服务已在运行：{info['host']} pid {info['pid']}")
        return
    core.ensure_dirs()
    log = open(os.path.join(core.HOME, "daemon.out"), "a")
    subprocess.Popen([sys.executable, "-m", "crewlib.daemon"], cwd=HERE, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     start_new_session=True, env={**os.environ, "PYTHONPATH": HERE})
    for _ in range(40):
        time.sleep(0.25)
        info = core.daemon_info()
        if info and info["alive"]:
            # 后台服务自成会话（start_new_session），和服务器的 sshd 同一个 cgroup / 进程命名空间：Claude 关掉不会带走它
            note = ""
            out({"ok": True, **info, "note": note}, a.json) if a.json else print(f"后台服务已启动：{info['host']} pid {info['pid']}{note}")
            return
    print("后台服务没有起来，看 ~/.crew/daemon.out", file=sys.stderr)
    sys.exit(1)


def ensure_up():
    info = core.daemon_info()
    if not (info and info["alive"]):
        # 提示写到 stderr：调用方要的是 stdout 上的 JSON（插件、工作流运行器）
        with contextlib.redirect_stdout(sys.stderr):
            cmd_up(argparse.Namespace(json=False))


def cmd_down(a):
    core.ensure_dirs()
    # 有意停掉：守护（工作流运行器、登录时的检查）不再把它拉起来；下一次 crew up / 派任务会清掉这个标记
    open(os.path.join(core.HOME, "stopped"), "w").close()
    info = core.daemon_info()
    if not (info and info["alive"]):
        print("后台服务没有在运行")
        return
    open(os.path.join(core.CTL, "stop"), "w").close()
    for _ in range(140):
        time.sleep(0.25)
        if not core.daemon_info():
            print("后台服务已停止；运行中的任务下次启动时续跑")
            return
    print("停止信号已发出，后台服务还在收尾（最多等 30 秒）")


def cmd_restart(a):
    """在后台服务所在的机器上原地重启、换上新代码：运行中的任务被打断后排队续跑（已做完的回合不丢）。
    后台服务平时也会在空闲时自己换新代码；这个命令用于等不及的时候。"""
    info = core.daemon_info()
    if not (info and info["alive"]):
        cmd_up(a)
        return
    core.ensure_dirs()
    open(os.path.join(core.CTL, "restart"), "w").close()
    for _ in range(240):
        time.sleep(0.25)
        now = core.daemon_info()
        if now and now["alive"] and now.get("started") != info.get("started"):
            print(f"后台服务已在 {now['host']} 原地重启（pid {now['pid']}），运行中的任务排队续跑")
            return
    if os.path.exists(os.path.join(core.CTL, "restart")):
        os.remove(os.path.join(core.CTL, "restart"))
        raise SystemExit(f"后台服务（{info['host']} pid {info['pid']}）60 秒没有响应重启指令，可能卡住了：看 ~/.crew/daemon.log；"
                         f"必要时 kill {info['pid']} 后 crew up（任务进度都在磁盘上，会续跑）")
    print("重启指令已发出，后台服务还在收尾（运行中的工人最多等 30 秒）")


# ───────── 任务
def task_model(model, profile=None):
    """任务显式指定的模型原样传给后端；省略时使用后端默认。"""
    return model or None


def deliverable_fields(spec, cwd):
    d = spec.get("deliverable")
    if not d:
        return {}
    paths = [d] if isinstance(d, str) else [str(x) for x in d if x]
    paths = [os.path.abspath(os.path.join(cwd, os.path.expanduser(x))) for x in paths]
    try:
        n = int(spec.get("nudge_after") or 15)
    except (TypeError, ValueError):
        raise SystemExit(f"nudge_after 要是回合数（整数）：{spec.get('nudge_after')}")
    return {"deliverable": paths, "nudge_after": max(1, n)}


def make_task(spec, cfg, owner):
    """校验并组好一个任务（还不落盘）；不合格直接报错退出。"""
    d = cfg["defaults"]
    cwd = os.path.abspath(os.path.expanduser(spec.get("cwd") or os.getcwd()))
    t = {
        "id": None, "title": spec.get("title") or spec["prompt"].strip().split("\n")[0][:60],
        "prompt": spec["prompt"], "accept": spec.get("accept") or "", "files": spec.get("files") or [],
        "schema": spec.get("schema"), "system": spec.get("system"), "cwd": cwd,
        # 未指定档案时使用默认工人。
        "profile": core.profile_name(spec.get("profile") or ("gpt" if re.match(r"(?i)^(gpt|o\d)", str(spec.get("model") or "")) and "gpt" in cfg["profiles"] else None), cfg),
        **({"context_tokens": int(spec["context_tokens"])} if spec.get("context_tokens") else {}),
        "owner": spec.get("owner") or owner, "group": spec.get("group") or "", "priority": int(spec.get("priority") or 0),
        "wf": spec.get("wf"),
        "model": task_model(spec.get("model"), core.profile_name(spec.get("profile"), cfg)), "effort": spec.get("effort") or None,
        # 截止日（回测）：工人只能用这天及以前的信息；web_fetch 只给 archive.org 上这天之前的存档
        **({"cutoff": str(spec["cutoff"])[:10]} if spec.get("cutoff") else {}),
        # 成果文件：到 nudge_after 回合（默认 15）还不存在，crew 自动插话一次“先把已查到的写进去”
        **deliverable_fields(spec, cwd),
        "status": "queued", "round": 1, "turns": 0, "usage": {"in": 0, "out": 0, "cached": 0},
        "created": core.now(), "updated": core.now(), "note": "已派发",
    }
    if t["effort"]:
        try:
            core.fit_effort(t["effort"])
        except ValueError as e:
            raise SystemExit(str(e))
    if t["profile"] not in cfg["profiles"]:
        raise SystemExit(f"没有这个模型档案：{t['profile']}（可选：{', '.join(cfg['profiles'])}）")
    t["origin"] = core.make_origin(t["owner"], spec.get("origin"))
    return t


def save_new_task(t):
    """落盘：目录独占创建，id 撞上了就换一个，绝不覆盖已有任务。"""
    core.ensure_dirs()
    while True:
        tid = core.new_id()
        try:
            os.mkdir(core.task_dir(tid))
            break
        except FileExistsError:
            continue
    t["id"] = tid
    core.write_json(os.path.join(core.task_dir(tid), "task.json"), t)
    core.append_event(tid, "status", frm=None, to="queued", note=f"由 {core.origin_label(t)} 派发")
    return t


def cmd_run(a):
    cfg = core.config()
    if a.stdin:
        payload = json.load(sys.stdin)
        specs = payload if isinstance(payload, list) else payload.get("tasks", [payload])
    else:
        if not a.prompt:
            raise SystemExit("要写任务内容：crew run \"…\"，或 --stdin 传 JSON")
        specs = [{"prompt": " ".join(a.prompt), "title": a.title, "accept": a.accept, "cwd": a.cwd, "profile": a.profile,
                  "files": a.file or [], "model": a.model, "effort": a.effort, "cutoff": a.cutoff,
                  "schema": json.load(open(a.schema)) if a.schema else None}]
    # 先把一批全部校验完再落盘：有一个不合格就一个都不建，不会留下半批没人管的任务
    built = [make_task(s, cfg, a.owner) for s in specs]
    batch = "b-" + core.new_id()      # 同一次派发的一批：插件等这一批都做完再一起叫醒 Claude
    for t in built:
        t["batch"] = batch
    made = [save_new_task(t) for t in built]
    ensure_up()
    if a.json:
        out([{"id": t["id"], "title": t["title"], "profile": t["profile"]} for t in made], True)
    else:
        for t in made:
            print(f"{t['id']}  {t['title']}  （{t['profile']}）")
    if a.wait:
        wait_for([t["id"] for t in made], a.timeout, a.json, all_=True)


def row(t, cfg=None):
    c = core.cost(t, cfg)
    u = t.get("usage") or {}
    return {
        "id": t["id"], "title": t["title"], "status": t["status"], "round": t.get("round", 1),
        "turns": t.get("turns", 0), "tokens": u.get("in", 0) + u.get("out", 0),
        "cached": u.get("cached", 0), "cost": round(c, 4) if c is not None else None, "elapsed": int(core.elapsed(t)),
        "owner": t.get("owner"), "origin": core.origin_label(t), "session": (t.get("origin") or {}).get("session") or "",
        "group": t.get("group"), "note": t.get("note", ""), "profile": t.get("profile"),
        "question": (t.get("question") or {}).get("text"), "needs_since": t.get("needs_since"),
        "finished": t.get("finished"), "updated": t.get("updated"),
        "wf": (t.get("wf") or {}).get("run") or "", "batch": t.get("batch") or "",
        "model": core.model_label(t, core.config()),
        **(_step(t["id"]) if t["status"] == "running" else {}),
    }


def _step(tid):
    """运行中的任务正在干什么（Claude 里输入框上方的进度栏用）：最近一次工具调用 / 说的话。"""
    from .wf import step_of
    s, ts = step_of(tid)
    return {"step": s[:200], "step_ago": int(core.now() - ts) if ts else None}


def select(a):
    tasks = core.list_tasks()
    if getattr(a, "owner", None):
        tasks = [t for t in tasks if t.get("owner") == a.owner]
    if getattr(a, "group", None):
        tasks = [t for t in tasks if t.get("group") == a.group]
    if getattr(a, "here", False):
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
        if not sid:
            raise SystemExit("--here 要在 Claude 对话的 Bash 里用（读环境变量 CLAUDE_CODE_SESSION_ID）")
        tasks = [t for t in tasks if (t.get("origin") or {}).get("session") == sid or t.get("owner") == f"claude:{sid[:8]}"]
    if getattr(a, "ids", None):
        want = {core.resolve_id(i) for i in a.ids}
        tasks = [t for t in tasks if t["id"] in want]
    elif not getattr(a, "all", False):
        cutoff = core.now() - 86400
        tasks = [t for t in tasks if t["status"] not in core.FINAL or t.get("updated", 0) > cutoff]
    return tasks


def cmd_ls(a):
    cfg = core.config()
    rows = [row(t, cfg) for t in select(a)]
    info = core.daemon_info()
    if a.json:
        from . import wf
        wfs = [w for w in (wf.load(i) for i in wf.list_ids()) if w]
        wfs = [wf.row(w) for w in wfs if w["status"] not in wf.DONE or core.now() - (w.get("finished") or 0) < 86400 or getattr(a, "all", False)]
        out({"daemon": info, "tasks": rows, "workflows": wfs, "alerts": core.list_alerts()}, True)
        return
    hidden = [r for r in rows if r["wf"]]
    if hidden and not a.all:
        rows = [r for r in rows if not r["wf"]]
    print(f"后台服务：{'运行中 ' + info['host'] + ' pid ' + str(info['pid']) if info and info['alive'] else '未运行（crew up 启动）'}")
    for al in core.list_alerts():
        if al["status"] == "open":
            print(f"⚠ 故障：{alert_line(al)}")
    if hidden and not a.all:
        print(f"（工作流里的 {len(hidden)} 个代理任务没列出：crew wf ls 看工作流，--all 一起列）")
    if not rows:
        print("没有任务")
        return
    print(f"{core.pad('ID', 16)}{core.pad('状态', 8)}{core.pad('轮', 3)}{core.pad('回合', 9)}{core.pad('tokens', 8)}{core.pad('用时', 8)}标题〔派发来源〕")
    for r in rows:
        print(f"{core.pad(r['id'], 16)}{core.pad(core.STATUS_ZH.get(r['status'], r['status']), 8)}{core.pad(str(r['round']), 3)}"
              f"{core.pad(str(r['turns']), 9)}{core.pad(core.fmt_tokens(r['tokens']), 8)}{core.pad(core.fmt_secs(r['elapsed']), 8)}"
              f"{r['title']}〔{r['origin']}〕")


def alert_line(a):
    when = time.strftime("%H:%M", time.localtime(a["first"]))
    st = {"open": f"自 {when} 起", "resolved": f"{when} 起，{time.strftime('%H:%M', time.localtime(a.get('resolved') or 0))} 恢复", "quiet": f"{when} 起，2 小时没再出现"}[a["status"]]
    return f"{a['title']}（{st}，{a['count']} 次，波及 {len(a.get('tasks') or [])} 个任务{'' if a.get('important') else '，次要'}；来源：{a['source']}）"


def cmd_alerts(a):
    """故障告警：列出；--resolve 手动解除（修好了、但巡检没覆盖到的）。"""
    if a.delete:
        done = core.delete_alerts(a.delete)
        if a.json:
            out({"deleted": done}, True)
        else:
            print(f"已删除：{', '.join(done)}" if done else f"没有 key 为 {', '.join(a.delete)} 的告警")
        return
    if a.resolve:
        done = core.resolve_alerts(a.resolve, a.why or "手动解除")
        if a.json:
            out({"resolved": done}, True)
        else:
            print(f"已解除：{', '.join(done)}" if done else f"没有开着的告警匹配 {a.resolve}")
        return
    al = core.list_alerts(within=86400 * (3 if a.all else 1))
    if a.json:
        out(al, True)
        return
    if not al:
        print("没有故障告警")
    for x in al:
        print(f"{'⚠' if x['status'] == 'open' else '✓' if x['status'] == 'resolved' else '·'} {alert_line(x)}\n    {x.get('sample', '')[:200]}"
              + (f"\n    任务：{' '.join(x['tasks'][:8])}" if x.get("tasks") else ""))


def origin_text(t, fresh=True):
    """派发来源的完整说明：对话标题（fresh 时按记录文件现在的标题）、第一句话、会话 id、怎么回到那个对话。"""
    o = dict(t.get("origin") or {})
    if not o.get("session") and str(t.get("owner") or "").startswith("claude:") and fresh:
        o.update(core.session_info(t["owner"][7:]))        # 加派发来源之前派的任务：按派发者里的会话 id 开头去找
    if not o.get("session"):
        return f"派发来源：{core.origin_label(t)}（不是从 Claude 对话派的）" + (f"，目录 {o['cwd']}" if o.get("cwd") else "")
    if fresh:
        o.update({k: v for k, v in core.session_info(o["session"]).items() if v})
    lines = [f"派发来源：对话「{o.get('title') or o.get('first') or '（无标题）'}」"]
    if core.via_label(o.get("via")):
        lines.append(f"　经由：{core.via_label(o['via'])}" + (f"　运行编号 {o['via']['run_id']}" if o["via"].get("run_id") else ""))
    owner = str(t.get("owner") or "")
    if owner and not owner.startswith("claude:") and owner != "cli":
        lines.append(f"　经脚本：{owner}")
    if o.get("first") and o.get("title"):
        lines.append(f"　对话第一句：{o['first']}")
    lines.append(f"　对话目录：{o.get('cwd', '—')}　机器：{o.get('host', '—')}　回到这个对话：claude --resume {o['session']}")
    return "\n".join(lines)


def cmd_show(a):
    tid = core.resolve_id(a.id)
    t = core.load_task(tid)
    res = core.read_json(os.path.join(core.task_dir(tid), "result.json"))
    if a.json:
        out({"task": {**row(t), "prompt": t["prompt"], "accept": t.get("accept"), "cwd": t["cwd"], "reviews": t.get("reviews", []), "origin_detail": t.get("origin"),
                      "question": t.get("question"), "files": t.get("files")}, "result": res}, True)
        return
    print(f"{t['id']} · {t['title']} · {core.STATUS_ZH.get(t['status'])} · 第 {t.get('round', 1)} 轮 · {t.get('turns', 0)} 回合 · {core.fmt_tokens(row(t)['tokens'])} tokens")
    print(f"工作目录：{t['cwd']}\n档案：{t['profile']}　派发者：{t.get('owner')}\n{origin_text(t)}\n备注：{t.get('note', '')}\n")
    print("── 任务\n" + t["prompt"])
    if t.get("accept"):
        print("\n── 验收标准\n" + t["accept"])
    if t.get("question"):
        print("\n── 待回答的提问\n" + t["question"]["text"] + ("\n选项：" + " / ".join(t["question"].get("options") or []) if t["question"].get("options") else ""))
    if res:
        print(f"\n── 成果（第 {res.get('round')} 轮）\n{res.get('summary', '')}")
        if res.get("files"):
            print("文件：\n" + "\n".join("  " + f for f in res["files"]))
        if res.get("self_check"):
            print("自查：" + res["self_check"])
        if res.get("open_issues"):
            print("未解决：\n" + "\n".join("  - " + x for x in res["open_issues"]))
        if res.get("data") is not None:
            print("数据：" + json.dumps(res["data"], ensure_ascii=False)[:3000])
    for r in t.get("reviews") or []:
        print(f"\n── 验收（第 {r.get('round')} 轮，{r.get('by')}）：{r['verdict']} {r.get('score') or ''}\n{r.get('text', '')}")


def digest_text(t, res):
    """交给 Claude 的成果：状态一行、工人的完整汇报、全部文件（带大小）、自查、未解决、data、上一轮验收意见。不截断。
    任务原文和验收标准是 Claude 自己写的，不再重复给（要看用 crew show 或 inspect part=task）。"""
    tid = t["id"]
    def clip(s, n=None):
        return str(s or "").strip()
    def size(p):
        try:
            n = os.path.getsize(p)
        except OSError:
            return "不存在"
        return f"{n / 1e6:.1f}MB" if n >= 1e6 else f"{n / 1e3:.0f}KB" if n >= 1e3 else f"{n}B"
    r = row(t)
    lines = [f"{tid}「{t['title']}」· {core.STATUS_ZH.get(t['status'], t['status'])} · {core.model_label(t, core.config())} · 第 {t.get('round', 1)} 轮 · {t.get('turns', 0)} 回合 · {core.fmt_tokens(r['tokens'])} tok · {core.fmt_secs(r['elapsed'])}"]
    if t.get("question"):
        q = t["question"]
        lines.append("提问：" + clip(q.get("text"), 600) + ("（选项：" + " / ".join(q.get("options") or []) + "）" if q.get("options") else ""))
    if t["status"] == "failed":
        lines.append("失败原因：" + clip(t.get("note"), 300))
    if res:
        if res.get("auto"):
            lines.append("⚠ 工人没有自己提交，下面是 crew 自动整理的（未经工人自查）")
        lines.append("汇报：\n" + clip(res.get("summary")))
        files = res.get("files") or []
        if files:
            shown = files
            base = os.path.commonpath([os.path.dirname(f) for f in shown]) if all(os.path.isabs(f) for f in shown) else ""
            rel = (lambda f: os.path.relpath(f, base)) if base and base != "/" else (lambda f: f)
            lines.append(f"文件（{len(files)}）" + (f"，都在 {base}/ 下" if base and base != "/" else "") + "：" + "；".join(f"{rel(f)}（{size(f)}）" for f in shown) )
        if res.get("self_check"):
            lines.append("自查：" + clip(res["self_check"], 300))
        issues = res.get("open_issues") or []
        if issues:
            lines.append(f"未解决（{len(issues)}）：\n" + "\n".join(f"- {clip(x)}" for x in issues))
        if res.get("data") is not None:
            lines.append("数据：" + json.dumps(res["data"], ensure_ascii=False, indent=1))
    elif t["status"] not in ("failed", "waiting"):
        lines.append("还没有成果。")
    revs = t.get("reviews") or []
    if revs:
        v = revs[-1]
        lines.append(f"上次验收（第 {v.get('round')} 轮）：{v.get('verdict')} " + clip(v.get("text"), 300))
    return "\n".join(lines)


def cmd_digest(a):
    parts = []
    for i in a.ids:
        tid = core.resolve_id(i, a.as_)
        t = core.load_task(tid)
        parts.append(digest_text(t, core.read_json(os.path.join(core.task_dir(tid), "result.json"))))
    print("\n\n".join(parts))


def cmd_claim(a):
    out(core.claim(a.owner, a.keys, release=a.release), True)


def cmd_task(a):
    tid = core.resolve_id(a.id)
    t = core.load_task(tid)
    print(f"── 任务\n{t['prompt']}" + (f"\n\n── 验收标准\n{t['accept']}" if t.get("accept") else ""))


def chat_items(t, tail=80):
    """任务的来回对话：派活原文、工人每次说的话 / 提问 / 提交、Claude 或你的插话 / 回答 / 验收。
    工人的工具调用不逐条列，合并成“执行了 N 次命令”。返回 [{who, label, head, text, t}]，who = claude / worker / you / system。"""
    from .worker import opening
    tid = t["id"]
    msgs = core.read_json(os.path.join(core.task_dir(tid), "msgs.json"), [])
    first = next((m["content"] for m in msgs if m.get("role") == "user"), None) or opening(t)
    o = t.get("origin") or {}
    owner = str(t.get("owner") or "")
    lead = ("claude", "Claude") if (o.get("session") or owner.startswith("claude:")) else ("you", "你" if owner in ("你", "cli") else owner)

    def speaker(frm):
        return ("you", "你") if frm == "你" else ("claude", "Claude")

    items = [{"who": lead[0], "label": lead[1], "head": "派活", "text": first, "t": t.get("created")}]
    tools = []

    def flush():
        if tools:
            names = {}
            for n in tools:
                names[n] = names.get(n, 0) + 1
            items.append({"who": "worker", "label": "工人", "head": "", "tools": True, "t": None,
                          "text": f"（执行了 {len(tools)} 次命令：" + "、".join(f"{k} ×{v}" for k, v in names.items()) + "）"})
            tools.clear()
    verdict_zh = {"accept": "通过", "revise": "退回修改", "reject": "否决"}
    for e in core.read_events(tid, tail=20000, max_bytes=8 << 20):
        k = e["k"]
        if k == "tool":
            tools.append(e.get("name") or "?")
            continue
        if k in ("out", "turn", "compact", "retry"):
            continue
        if k == "say" and e.get("text", "").strip():
            flush()
            items.append({"who": "worker", "label": "工人", "head": "", "text": e["text"], "t": e["t"]})
        elif k == "ask":
            flush()
            opts = e.get("options") or []
            items.append({"who": "worker", "label": "工人", "head": "提问", "text": e.get("text", "") + (("\n选项：" + " / ".join(opts)) if opts else ""), "t": e["t"]})
        elif k == "submit":
            flush()
            files = e.get("files") or []
            items.append({"who": "worker", "label": "工人", "head": f"提交第 {e.get('round')} 轮", "t": e["t"],
                          "text": (e.get("summary") or "") + (("\n文件：\n" + "\n".join("  " + f for f in files)) if files else "")})
        elif k in ("steer", "answer") and e.get("frm"):
            flush()
            who, label = speaker(e["frm"])
            items.append({"who": who, "label": label, "head": "回答" if k == "answer" else "插话", "text": e.get("text", ""), "t": e["t"]})
        elif k == "review":
            flush()
            who, label = speaker(e.get("frm"))
            sc = f"（{e['score']} 分）" if e.get("score") not in (None, "") else ""
            items.append({"who": who, "label": label, "head": f"验收：{verdict_zh.get(e.get('verdict'), e.get('verdict'))}{sc}", "text": e.get("text", ""), "t": e["t"]})
        elif k == "status" and e.get("to") in ("failed", "accepted", "rejected", "cancelled", "paused"):
            flush()
            items.append({"who": "system", "label": "crew", "head": core.STATUS_ZH.get(e["to"], e["to"]), "text": e.get("note", ""), "t": e["t"]})
        elif k == "error":
            flush()
            items.append({"who": "system", "label": "crew", "head": "出错", "text": e.get("text", ""), "t": e["t"]})
        elif k == "alert":
            flush()
            items.append({"who": "system", "label": "crew", "head": "⚠ 故障", "text": e.get("text", ""), "t": e["t"]})
    flush()
    if len(items) > tail + 1:
        items = items[:1] + [{"who": "system", "label": "crew", "head": "", "t": None,
                              "text": f"（更早的 {len(items) - tail - 1} 条省略；全部：crew chat {tid} -n 0）"}] + items[-tail:]
    return items


def convo_blocks(t):
    """面板“对话”页的完整内容，按时间排：派活原文、每一回合工人的推理 / 说的话 / 工具调用和输出，
    Claude 或你的插话、回答、验收，crew 的状态变化。返回 [{kind, who, head, text, t}]，
    kind = lead / think / say / tool / out / turn / ask / submit / system。"""
    from .worker import opening
    tid = t["id"]
    msgs = core.read_json(os.path.join(core.task_dir(tid), "msgs.json"), [])
    first = next((m["content"] for m in msgs if m.get("role") == "user"), None) or opening(t)
    o = t.get("origin") or {}
    owner = str(t.get("owner") or "")
    lead = "Claude" if (o.get("session") or owner.startswith("claude:")) else ("你" if owner in ("你", "cli") else owner)
    out = [{"kind": "lead", "who": lead, "head": "派活", "text": str(first), "t": t.get("created")}]
    verdict_zh = {"accept": "通过", "revise": "退回修改", "reject": "否决"}
    for e in core.read_events(tid, tail=10 ** 6, max_bytes=64 << 20):
        k, ts = e["k"], e.get("t")
        if k == "turn":
            out.append({"kind": "turn", "head": f"回合 {e.get('n')}", "t": ts,
                        "text": f"入 {core.fmt_tokens(e.get('tok_in'))} · 出 {core.fmt_tokens(e.get('tok_out'))} · 累计 {core.fmt_tokens(e.get('total'))}"})
        elif k == "think" and e.get("text", "").strip():
            out.append({"kind": "think", "who": "工人", "head": "推理", "text": e["text"], "t": ts})
        elif k == "say" and e.get("text", "").strip():
            out.append({"kind": "say", "who": "工人", "head": "", "text": e["text"], "t": ts})
        elif k == "tool":
            out.append({"kind": "tool", "who": "工人", "head": e.get("name") or "?", "text": e.get("full") or e.get("arg") or "", "t": ts})
        elif k == "out":
            n = e.get("chars") or len(e.get("text") or "")
            txt = e.get("text") or ""
            more = ("（这里只显示开头，全文：" + e["full_path"] + "）") if e.get("full_path") else "（这里只显示开头；工人拿到的是全文，这条记录早于全文存档）"
            out.append({"kind": "out", "head": ("✓" if not e.get("code") else f"✗ {e.get('code')}") + f"  {n} 字" + (more if n > len(txt) else ""),
                        "text": txt, "t": ts, "ok": not e.get("code")})
        elif k == "ask":
            opts = e.get("options") or []
            out.append({"kind": "ask", "who": "工人", "head": "提问", "text": e.get("text", "") + (("\n选项：" + " / ".join(opts)) if opts else ""), "t": ts})
        elif k == "submit":
            files = e.get("files") or []
            out.append({"kind": "submit", "who": "工人", "head": f"提交第 {e.get('round')} 轮", "t": ts,
                        "text": (e.get("summary") or "") + (("\n文件：\n" + "\n".join("  " + f for f in files)) if files else "")})
        elif k in ("steer", "answer") and e.get("frm"):
            out.append({"kind": "lead", "who": "你" if e["frm"] == "你" else e["frm"], "head": "回答" if k == "answer" else "插话", "text": e.get("text", ""), "t": ts})
        elif k == "review":
            sc = f"（{e['score']} 分）" if e.get("score") not in (None, "") else ""
            out.append({"kind": "lead", "who": "你" if e.get("frm") == "你" else (e.get("frm") or "Claude"),
                        "head": f"验收：{verdict_zh.get(e.get('verdict'), e.get('verdict'))}{sc}", "text": e.get("text", ""), "t": ts})
        elif k == "status" and e.get("to") not in ("running",):
            out.append({"kind": "system", "head": core.STATUS_ZH.get(e.get("to"), e.get("to")), "text": e.get("note") or "", "t": ts})
        elif k in ("error", "alert"):
            out.append({"kind": "system", "head": "出错" if k == "error" else "⚠ 故障", "text": e.get("text", ""), "t": ts, "bad": True})
        elif k == "compact":
            out.append({"kind": "system", "head": "压缩上下文", "text": f"{core.fmt_tokens(e.get('before'))} → {core.fmt_tokens(e.get('after'))}", "t": ts})
        elif k == "retry":
            out.append({"kind": "system", "head": f"接口重试 {e.get('n')}", "text": f"等 {e.get('wait')} 秒：{core.clip(e.get('err') or '', 160)}", "t": ts, "retry": True})
    return out


def cmd_chat(a):
    tid = core.resolve_id(a.id)
    t = core.load_task(tid)
    items = chat_items(t, tail=a.tail if a.tail > 0 else 10 ** 9)
    if a.json:
        out({"id": tid, "items": items}, True)
        return
    tty = sys.stdout.isatty()
    color = {"claude": "38;5;173", "worker": "38;5;69", "you": "38;5;71", "system": "2"}
    for it in items:
        name = it["label"] + "：" + (f"{it['head']}  " if it.get("head") else "") + (core.stamp(it["t"]) if it.get("t") else "")
        print(f"\033[1;{color[it['who']]}m{name}\033[0m" if tty else name)
        for ln in str(it["text"]).split("\n"):
            print((f"\033[{color[it['who']]}m▎\033[0m " if tty else "▎ ") + ln)
        print()


def cmd_brief(a):
    """派活上下文：谁派的、工人收到的第一条消息原文、之后组长追加的指示（插话、回答、退回意见）。"""
    from .worker import SYSTEM, opening
    tid = core.resolve_id(a.id)
    t = core.load_task(tid)
    msgs = core.read_json(os.path.join(core.task_dir(tid), "msgs.json"), [])
    first = next((m["content"] for m in msgs if m.get("role") == "user"), None) or opening(t)
    later = [fmt_event(e) for e in core.read_events(tid, tail=2000) if e["k"] in ("steer", "answer", "review")]
    sid = (t.get("origin") or {}).get("session") or ""
    if not sid and str(t.get("owner") or "").startswith("claude:"):
        path = core.transcript_path(t["owner"][7:])      # 加派发来源之前派的任务：按派发者里的会话 id 开头找完整 id
        sid = os.path.basename(path)[:-6] if path else ""
    res = {"id": tid, "title": t["title"], "status": t["status"], "origin": core.origin_label(t), "origin_text": origin_text(t), "session": sid,
           "profile": t.get("profile"), "cwd": t["cwd"], "opening": first,
           "system": t.get("system") or (SYSTEM + ("\n\n" + core.config().get("worker_env_note", "") if core.config().get("worker_env_note") else "")), "later": later}
    if a.json:
        out(res, True)
        return
    print(f"{tid} · {t['title']}\n{res['origin_text']}\n档案：{res['profile']}　工作目录：{res['cwd']}\n")
    print("── 工人收到的第一条消息\n" + first)
    if later:
        print("\n── 之后的追加指示\n" + "\n".join(later))


def fmt_event(e, width=10000):
    k = e["k"]
    ts = core.stamp(e["t"])
    if k == "turn":
        s = f"── 回合 {e['n']}  入 {core.fmt_tokens(e.get('tok_in'))} 出 {core.fmt_tokens(e.get('tok_out'))}  累计 {core.fmt_tokens(e.get('total'))}"
    elif k == "tool":
        s = f"▶ {e['name']}  {e.get('arg', '')}"
    elif k == "out":
        s = f"◀ {'✓' if e.get('code') == 0 else '✗ ' + str(e.get('code'))}  {e.get('text', '')}"
    elif k == "say":
        s = f"💬 {e['text']}"
    elif k == "ask":
        s = f"❓ 提问：{e['text']}" + (f"（{' / '.join(e.get('options') or [])}）" if e.get("options") else "")
    elif k == "answer":
        s = f"↳ 回答（{e.get('frm', '组长')}）：{e['text']}"
    elif k == "steer":
        s = f"↳ 插话（{e.get('frm', '组长')}）：{e['text']}"
    elif k == "submit":
        s = f"📦 提交第 {e['round']} 轮：{e.get('summary', '')}"
    elif k == "review":
        s = f"⚖ 验收 {e['verdict']} {e.get('score') or ''}（{e.get('frm', '')}）：{e.get('text', '')}"
    elif k == "status":
        s = f"● {core.STATUS_ZH.get(e.get('frm'), e.get('frm') or '—')} → {core.STATUS_ZH.get(e['to'], e['to'])}  {e.get('note', '')}"
    elif k == "compact":
        s = f"⇣ 压缩上下文 {e['before']}→{e['after']} 字符"
    elif k == "retry":
        s = f"↻ 接口重试 {e['n']}，等 {e['wait']} 秒：{e.get('err', '')}" if e.get("n") else f"↻ {e.get('err', '')}"
    elif k == "alert":
        s = f"⚠ 故障：{e.get('text', '')}"
    elif k == "model":
        s = f"⚙ 模型 {e.get('text', '')}"
    else:
        s = f"{k} {e.get('text', '')}"
    return f"{ts} {core.clip(s, width)}"


def cmd_log(a):
    tid = core.resolve_id(a.id)
    evs = core.read_events(tid, tail=a.tail)
    if a.json:
        out([fmt_event(e, a.width) for e in evs] if a.fmt else evs, True)
        return
    for e in evs:
        print(fmt_event(e, a.width))
    if a.follow:
        path = os.path.join(core.task_dir(tid), "events.jsonl")
        pos = os.path.getsize(path)
        try:
            while True:
                time.sleep(0.5)
                size = os.path.getsize(path)
                if size > pos:
                    with open(path, encoding="utf-8") as f:
                        f.seek(pos)
                        for line in f.read().splitlines():
                            try:
                                print(fmt_event(json.loads(line), a.width), flush=True)
                            except json.JSONDecodeError:
                                pass
                    pos = size
        except KeyboardInterrupt:
            pass


def cmd_transcript(a):
    tid = core.resolve_id(a.id)
    msgs = core.read_json(os.path.join(core.task_dir(tid), "msgs.json"), [])
    msgs = msgs[-a.tail:] if a.tail else msgs
    if a.json:
        out(msgs, True)
        return
    for m in msgs:
        body = str(m.get("content") or "")
        if m.get("tool_calls"):
            body += "\n" + "\n".join(f"  → {tc['function']['name']}({tc['function']['arguments'][:400]})" for tc in m["tool_calls"])
        print(f"[{m['role']}] {body[:a.chars]}\n")


def guard(t, who, what):
    """防串对话：Claude 只能操作本对话派的任务（插件调用时带 --as 本对话）。"""
    if who and not core.owned_by(t, who):
        raise SystemExit(f"{t['id']} 是{core.origin_label(t)}派的任务，不是本对话的，不能{what}。确实要管就让那个对话处理，或用户在面板里操作。")


def drop_cmd(kind, **extra):
    def f(a):
        who = getattr(a, "as_", None)
        tid = core.resolve_id(a.id, who)
        t = core.load_task(tid)
        guard(t, who, {"steer": "插话", "answer": "回答", "followup": "派下一步"}.get(kind, kind))
        payload = dict(extra)
        if hasattr(a, "text") and a.text is not None:
            payload["text"] = " ".join(a.text) if isinstance(a.text, list) else a.text
        payload["from"] = a.frm
        core.drop(tid, kind, **payload)
        ensure_up()
        msg = {"ok": True, "id": tid, "kind": kind, "status_before": t["status"]}
        out(msg, a.json) if a.json else print(f"已投递 {kind} → {tid}（当前状态：{core.STATUS_ZH.get(t['status'])}）")
    return f


def cmd_review(a):
    tid = core.resolve_id(a.id, a.as_)
    t = core.load_task(tid)
    guard(t, a.as_, "验收")
    if t["status"] not in ("review", "failed") and a.verdict != "reject":
        raise SystemExit(f"{tid} 当前状态是 {core.STATUS_ZH.get(t['status'])}，不能验收")
    text = " ".join(a.text or [])
    if a.verdict == "revise" and not text.strip():
        raise SystemExit("退回修改必须写具体意见")
    core.drop(tid, "review", verdict=a.verdict, score=a.score, text=text, frm=a.frm)
    ensure_up()
    msg = {"ok": True, "id": tid, "verdict": a.verdict, "round": t.get("round", 1)}
    out(msg, a.json) if a.json else print(f"已验收 {tid}：{a.verdict}")


def wait_for(ids, timeout, as_json, all_=False, owner=None):
    """等到任务需要组长处理（待验收 / 提问 / 失败）或结束。默认任意一个就返回。"""
    t0 = core.now()

    def ready(t):
        # 刚投递、后台服务还没处理的指令（回答、退回、重试）：状态马上会变，不算就绪
        try:
            if any(n.endswith(".json") for n in os.listdir(os.path.join(core.task_dir(t["id"]), "inbox"))):
                return False
        except FileNotFoundError:
            pass
        return t["status"] in core.NEEDS_LEAD or t["status"] in core.FINAL

    while True:
        if ids:
            pool = [t for t in (core.load_task(i) for i in ids) if t]
        else:
            # 不指定任务：等本人还在跑的，和等待期间新变成要处理的；早就在待验收里放着的旧任务不算（否则一调就返回）。
            # 工作流的代理任务由运行器管，不算。
            pool = [t for t in core.list_tasks() if not t.get("wf") and (not owner or t.get("owner") == owner)
                    and (t["status"] in core.ACTIVE or (t["status"] in core.NEEDS_LEAD and float(t.get("needs_since") or 0) >= t0))]
        done = [t for t in pool if ready(t)]
        pending = [t for t in pool if not ready(t)]
        if (done and not all_) or not pending or core.now() - t0 >= timeout:
            res = {"ready": [row(t) for t in done], "pending": [row(t) for t in pending], "timed_out": bool(pending) and not (done and not all_) and core.now() - t0 >= timeout}
            if as_json:
                out(res, True)
            else:
                for r in res["ready"]:
                    print(f"{r['id']}  {core.STATUS_ZH.get(r['status'])}  {r['title']}" + (f"  提问：{r['question']}" if r.get("question") else ""))
                if res["pending"]:
                    print(f"（还有 {len(res['pending'])} 个在跑）")
            return res
        time.sleep(1)


def cmd_wait(a):
    ids = [core.resolve_id(i) for i in a.ids] if a.ids else None
    wait_for(ids, a.timeout, a.json, all_=a.all, owner=a.owner)


def cmd_rm(a):
    import shutil
    ids = list(a.ids)
    if a.failed:
        ids += [t["id"] for t in core.list_tasks() if t["status"] == "failed"]
    if not ids:
        print("没有要删的任务" + ("（没有失败的任务）" if a.failed else "：crew rm <id>… 或 crew rm --failed"))
        return
    for i in dict.fromkeys(ids):
        tid = core.resolve_id(i)
        t = core.load_task(tid)
        run = (t.get("wf") or {}).get("run")
        if run and t["status"] not in core.FINAL and (core.read_json(os.path.join(core.HOME, "workflows", run, "state.json")) or {}).get("status") in ("starting", "running"):
            print(f"{tid} 是运行中的工作流 {run} 的代理，运行器还要用它；先 crew wf cancel {run}")
            continue
        if t["status"] in core.ACTIVE:
            if not a.force:
                print(f"{tid} 还在 {core.STATUS_ZH.get(t['status'])}，先 crew cancel，或加 --force")
                continue
            # 先叫停工人，等它收尾再删：否则工人下一次写状态会把目录又建出来
            core.drop(tid, "cancel", frm="你")
            ensure_up()
            for _ in range(120):
                if (core.load_task(tid) or {}).get("status") not in core.ACTIVE:
                    break
                time.sleep(0.25)
        shutil.rmtree(core.task_dir(tid), ignore_errors=True)
        print(f"已删除 {tid}")


def cmd_top(a):
    from .tui import main as tui_main
    tui_main(a)


HELP = """crew — Claude 派活，GPT 工人（跑在本机 Codex 里）在后台干活

是什么
  Claude 把采集、整理、跑脚本、写初稿这类活派给 GPT 工人（每个任务一个 Codex 会话，模型由用户 Codex 决定）。
  后台服务独立于 Claude 运行：关掉 Claude、断开 SSH，任务和工作流照常跑，做完叫醒派活的那个对话。

三种用法
  任务     Claude 用 mcp__crew__dispatch 派；工人做完交成果，Claude 验收（通过 / 退回修改 / 否决）
  工作流   Claude 写好工作流脚本交给 crew 跑，每个 agent() 是一个工人；中断了自动续跑
  命令行   crew run "要做的事" 自己派（结果不会叫醒 Claude）

工人
  默认 gpt：使用 PATH 中已登录的 Codex，不覆盖模型；--model <模型> 原样传给 Codex
  默认最多 4 个工人，可在配置中调整；Claude 工人（-p claude）需明确指定
  gpt-chat 是可选的 API 档案，需配置 model 和 OPENAI_API_KEY

在终端看
  crew              打开面板（主页 / 任务 / 工作流），面板里 1–4 切页、Enter 整页打开、Esc 返回、4 看按键
  crew ls           列任务（本对话的：在 Claude 的 Bash 里加 --here）
  crew show <id>    任务详情；crew chat <id> 看工人和 Claude 的往来；crew log <id> 时间线
  crew wf ls        列工作流；crew wf watch <id> 实时进度
  crew alerts       数据源 / 服务故障

操作任务
  crew say <id> 话      插话（运行中的工人下一回合读到）
  crew answer <id> 话   回答工人的提问
  crew review <id> accept|revise|reject 意见
  crew pause|resume|cancel <id>；失败了 crew resume <id> --retry 从断点续跑
  crew rm <id> | --failed

后台服务
  crew up | down | restart     restart：原地换新代码，运行中的任务续跑

在 Claude 里
  /crew             打开面板（同终端主页）
  /crew help        这份说明
  /crew auto off    不自动叫醒 Claude（任务照常跑）

工人能用的工具
  bash、读写文件、用户配置的 MCP 服务、向派活的 Claude 提问、报告故障。
  数据源坏了（502、连不上），crew 会叫醒受影响的那个对话；只叫有关的对话。

状态
  排队 → 运行 → 待验收 → 通过；途中可能 提问（等回答）、暂停、失败（可续跑）、取消、否决

配置 ~/.crew/config.json · 说明见安装目录的 README.md
"""


def cmd_help(a):
    print(HELP)


def main(argv=None):
    p = argparse.ArgumentParser(prog="crew", description="Claude 派活，GPT 工人干活。不带参数 = 打开面板；crew help 看使用说明。")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("help", help="使用说明")
    s.set_defaults(f=cmd_help)
    s = sub.add_parser("effort", help="看 / 改各档案的默认思考档位：crew effort gpt high")
    s.add_argument("profile", nargs="?")
    s.add_argument("level", nargs="?")
    s.set_defaults(f=cmd_effort)
    s = sub.add_parser("models", help="列出一个档案接口上的模型并试发一条（仅 API 档案）")
    s.add_argument("profile", nargs="?", default="gpt")
    s.add_argument("filter", nargs="?", default="", help="只列 id 里含这段的")
    s.set_defaults(f=cmd_models)
    s = sub.add_parser("peers", help="本机正开着的 Claude 对话，和它们用没用 crew（跨对话发消息找收件人）")
    s.add_argument("--me", default="", help=argparse.SUPPRESS)
    s.set_defaults(f=cmd_peers)
    s = sub.add_parser("peer-log", help="读另一个对话的上下文（对方缓存过期时不发消息，用这个看它在做什么）")
    s.add_argument("to", help="名字 / 会话 id 前缀 / 标题里的词")
    s.add_argument("-n", type=int, default=40, help="最近几条")
    s.add_argument("--chars", type=int, default=600, help="每段文字最多几个字")
    s.add_argument("--bytes", type=int, default=8, help="从记录末尾读多少 MB")
    s.add_argument("--results", action="store_true", help="也列工具结果摘要")
    s.set_defaults(f=cmd_peer_log)
    s = sub.add_parser("msgs", help="跨对话消息记录")
    s.add_argument("-n", type=int, default=20)
    s.add_argument("--add", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(f=cmd_msgs)
    s = sub.add_parser("up", help="启动后台服务")
    s.add_argument("--watch", action="store_true", help=argparse.SUPPRESS)   # 守护用：有意停掉的不拉、运行中不出声
    s.set_defaults(f=cmd_up)
    s = sub.add_parser("down", help="停止后台服务（运行中的任务下次续跑）")
    s.set_defaults(f=cmd_down)
    s = sub.add_parser("restart", help="原地重启后台服务换上新代码（运行中的任务被打断后续跑）")
    s.set_defaults(f=cmd_restart)
    s = sub.add_parser("alerts", help="故障告警（数据源 / 服务坏了）：列出，--resolve 手动解除，--delete 删掉")
    s.add_argument("--resolve", help="要解除的告警 key 或地址片段")
    s.add_argument("--all", action="store_true", help="含 3 天内已解除的")
    s.add_argument("--why", default="", help="解除原因（和 --resolve 一起用）")
    s.add_argument("--delete", nargs="+", help="删掉这些 key 的告警（误报、或已解决不想再看到）")
    s.set_defaults(f=cmd_alerts)
    s = sub.add_parser("top", help="打开实时面板")
    s.set_defaults(f=cmd_top)
    s = sub.add_parser("run", help="派一个任务")
    s.add_argument("prompt", nargs="*")
    s.add_argument("-t", "--title")
    s.add_argument("-a", "--accept", help="验收标准")
    s.add_argument("-C", "--cwd")
    s.add_argument("-p", "--profile")
    s.add_argument("--model", help="后端支持的任意模型名；原样传递，省略时使用用户 CLI 默认")
    s.add_argument("-e", "--effort", help="思考档位 low / medium / high / xhigh / max（默认使用后端设置；crew effort 查看）")
    s.add_argument("--cutoff", help="截止日 YYYY-MM-DD（回测）：工人的 web_fetch 只给 archive.org 上这天之前的存档")
    s.add_argument("-f", "--file", action="append", help="先读的材料（可重复）")
    s.add_argument("--max-turns", type=int, help="已取消：工人不设回合上限（参数照收，不起作用）")
    s.add_argument("--schema", help="成果 data 字段的 JSON Schema 文件")
    s.add_argument("--owner", default="cli")
    s.add_argument("--stdin", action="store_true", help="从标准输入读任务 JSON（单个、列表或 {tasks:[…]}）")
    s.add_argument("--wait", action="store_true")
    s.add_argument("--timeout", type=float, default=3600)
    s.set_defaults(f=cmd_run)
    s = sub.add_parser("ls", help="列出任务")
    s.add_argument("--all", action="store_true", help="包括一天前结束的")
    s.add_argument("--owner")
    s.add_argument("--group")
    s.add_argument("--here", action="store_true", help="只列本对话派的（在 Claude 的 Bash 里用）")
    s.add_argument("ids", nargs="*")
    s.set_defaults(f=cmd_ls)
    s = sub.add_parser("show", help="看任务详情和成果")
    s.add_argument("id")
    s.set_defaults(f=cmd_show)
    s = sub.add_parser("digest", help="交给 Claude 看的成果（不截断）：完整汇报、全部文件、自查、未解决、data、上次验收")
    s.add_argument("ids", nargs="+")
    s.add_argument("--as", dest="as_")
    s.set_defaults(f=cmd_digest)
    s = sub.add_parser("claim", help=argparse.SUPPRESS)        # 插件用：唤醒去重
    s.add_argument("--owner", required=True)
    s.add_argument("--release", action="store_true")
    s.add_argument("keys", nargs="*")
    s.set_defaults(f=cmd_claim)
    s = sub.add_parser("task", help="任务原文和验收标准")
    s.add_argument("id")
    s.set_defaults(f=cmd_task)
    s = sub.add_parser("chat", help="看来回对话：Claude 派的活、工人的回复 / 提问 / 提交、插话与验收")
    s.add_argument("id")
    s.add_argument("-n", "--tail", type=int, default=80, help="最多显示最近多少条（0 = 全部）")
    s.set_defaults(f=cmd_chat)
    s = sub.add_parser("brief", help="看派活上下文：谁派的、工人收到的任务原文、之后的追加指示")
    s.add_argument("id")
    s.set_defaults(f=cmd_brief)
    s = sub.add_parser("log", help="看时间线")
    s.add_argument("id")
    s.add_argument("-n", "--tail", type=int, default=60)
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("-w", "--width", type=int, default=400)
    s.add_argument("--fmt", action="store_true", help="配合 --json：输出排好版的行")
    s.set_defaults(f=cmd_log)
    s = sub.add_parser("transcript", help="看工人与模型的完整对话")
    s.add_argument("id")
    s.add_argument("-n", "--tail", type=int, default=20)
    s.add_argument("--chars", type=int, default=3000)
    s.set_defaults(f=cmd_transcript)
    for name, kind, hlp in (("say", "steer", "给运行中的工人插话"), ("answer", "answer", "回答工人的提问"),
                            ("followup", "followup", "接着同一个工人的上下文派下一步（任务已交过成果时用）")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("id")
        s.add_argument("text", nargs="+")
        s.add_argument("--from", dest="frm", default="组长")
        s.add_argument("--as", dest="as_", help=argparse.SUPPRESS)
        s.set_defaults(f=drop_cmd(kind))
    for name, hlp in (("pause", "暂停"), ("resume", "继续"), ("cancel", "取消")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("id")
        s.add_argument("--from", dest="frm", default="组长")
        s.add_argument("--as", dest="as_", help=argparse.SUPPRESS)
        if name == "resume":
            s.add_argument("--retry", action="store_true", help="让失败的任务从断点续跑")
            s.set_defaults(f=lambda a: drop_cmd("resume", retry=a.retry)(a))
        else:
            s.set_defaults(f=drop_cmd(name))
    s = sub.add_parser("review", help="验收：accept / revise / reject")
    s.add_argument("id")
    s.add_argument("verdict", choices=["accept", "revise", "reject"])
    s.add_argument("text", nargs="*", help="意见（退回修改时必填，写具体）")
    s.add_argument("-s", "--score", type=float)
    s.add_argument("--from", dest="frm", default="组长")
    s.add_argument("--as", dest="as_", help=argparse.SUPPRESS)
    s.set_defaults(f=cmd_review)
    s = sub.add_parser("wait", help="等到有任务需要处理（待验收 / 提问 / 失败）")
    s.add_argument("ids", nargs="*")
    s.add_argument("--all", action="store_true", help="等全部就绪")
    s.add_argument("--owner")
    s.add_argument("--timeout", type=float, default=300)
    s.set_defaults(f=cmd_wait)
    from . import wf as _wf
    _wf.add_parser(sub)
    s = sub.add_parser("rm", help="删除任务目录（--failed 删掉全部失败的任务）")
    s.add_argument("ids", nargs="*")
    s.add_argument("--failed", action="store_true", help="删除全部失败的任务")
    s.add_argument("--force", action="store_true")
    s.set_defaults(f=cmd_rm)
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv            # 子命令前后都认
    a, extra = p.parse_known_args([x for x in argv if x != "--json"])
    if extra:
        # 选项写在正文后面（crew review ID revise -s 5 "意见"）：多出来的词并回正文
        field = next((f for f in ("text", "prompt") if isinstance(getattr(a, f, None), list)), None)
        if not field:
            p.error("不认识的参数：" + " ".join(extra))
        setattr(a, field, getattr(a, field) + extra)
    a.json = as_json
    if not a.cmd:
        a.f = cmd_top
    a.f(a)


if __name__ == "__main__":
    main()
