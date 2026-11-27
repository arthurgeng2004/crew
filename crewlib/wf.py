"""crew 托管的工作流：Claude 写脚本（Claude Code Workflow 的写法），crew 用 Node 跑，每个 agent() 是一个 DeepSeek 工人任务。

  workflows/<id>/run.json      运行规格（这里写）        script.js   脚本副本
  workflows/<id>/state.json    进度快照（运行器写）      journal.jsonl  每个 agent() 的开始 / 结果（续跑用）
  workflows/<id>/result.json   脚本返回值               proc.json   运行器进程（后台服务写）
  workflows/<id>/ctl/start     请后台服务拉起运行器      ctl/cancel  请运行器取消
"""
import json
import os
import shutil
import subprocess
import sys
import time

from . import core

WF = os.path.join(core.HOME, "workflows")
RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wfrun.mjs")
DONE = ("done", "failed", "cancelled")
ZH = {"starting": "启动中", "running": "运行", "done": "完成", "failed": "失败", "cancelled": "取消"}
AZH = {"queued": "排队", "running": "运行", "paused": "暂停", "waiting": "提问", "retry": "重试", "done": "完成",
       "failed": "失败", "cancelled": "取消"}
GLYPH = {"queued": "…", "running": "▶", "paused": "‖", "retry": "↻", "done": "✓", "failed": "✗", "cancelled": "·", "waiting": "?"}


def wdir(wid):
    return os.path.join(WF, wid)


def node():
    return shutil.which("node") or "/usr/local/bin/node"


def check(script_path):
    r = subprocess.run([node(), RUNNER, "check", script_path], capture_output=True, text=True, timeout=60)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": (r.stderr or r.stdout or "检查失败").strip()[-600:]}


def create(spec, owner):
    """spec: {script | script_path, args, cwd, profile, concurrency, origin}。返回 run.json。"""
    os.makedirs(WF, exist_ok=True)
    wid = "wf-" + core.new_id()
    d = wdir(wid)
    os.makedirs(os.path.join(d, "ctl"))
    src = spec.get("script")
    if not src and spec.get("script_path"):
        with open(os.path.expanduser(spec["script_path"]), encoding="utf-8") as f:
            src = f.read()
    if not src:
        shutil.rmtree(d)
        raise SystemExit("要给 script（脚本正文）或 script_path")
    sp = os.path.join(d, "script.js")
    with open(sp, "w", encoding="utf-8") as f:
        f.write(src)
    c = check(sp)
    if not c.get("ok"):
        shutil.rmtree(d)
        raise SystemExit("工作流脚本有问题：" + c.get("error", ""))
    meta = c["meta"]
    owner = spec.get("owner") or owner
    run = {
        "id": wid, "name": meta["name"], "description": meta.get("description", ""), "phases": meta.get("phases") or [],
        "args": spec.get("args"), "cwd": os.path.abspath(os.path.expanduser(spec.get("cwd") or os.getcwd())),
        "profile": spec.get("profile") or None, "model": spec.get("model") or None, "effort": spec.get("effort") or None, "concurrency": int(spec.get("concurrency") or meta.get("concurrency") or core.config().get("wf_concurrency") or 0),
        "owner": owner, "origin": core.make_origin(owner, spec.get("origin")), "created": core.now(),
        "script_path": spec.get("script_path") or "",
    }
    core.write_json(os.path.join(d, "run.json"), run)
    open(os.path.join(d, "ctl", "start"), "w").close()
    return run


def load(wid):
    d = wdir(wid)
    run = core.read_json(os.path.join(d, "run.json"))
    if not run:
        return None
    st = core.read_json(os.path.join(d, "state.json")) or {}
    proc = core.read_json(os.path.join(d, "proc.json")) or {}
    w = {**run, **{k: v for k, v in st.items() if v is not None or k not in run}}
    w.setdefault("status", "starting")
    w.setdefault("agents", [])
    w.setdefault("logs", [])
    w["proc"] = proc
    if w["status"] == "running" and st.get("beat") and core.now() - st["beat"] > 30 and not pid_alive(proc.get("pid")):
        w["status"] = "starting"     # 运行器没了，后台服务会拉起续跑
    return w


def list_ids():
    try:
        return sorted(os.listdir(WF))
    except FileNotFoundError:
        return []


def resolve(prefix):
    ids = list_ids()
    if prefix in ids:
        return prefix
    hits = [i for i in ids if i.startswith(prefix) or i.endswith(prefix) or i[3:].startswith(prefix)]
    if len(hits) == 1:
        return hits[0]
    raise SystemExit(f"找不到唯一的工作流：{prefix}（匹配 {len(hits)} 个）")


def pid_alive(pid):
    if not pid:
        return False
    try:
        got, _ = os.waitpid(pid, os.WNOHANG)       # 是后台服务的子进程：顺手回收僵尸
        if got:
            return False
    except ChildProcessError:
        pass
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            if f.read().split(")")[-1].split()[0] == "Z":
                return False
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"wfrun" in f.read()       # 机器重启后 pid 可能被别的进程占了：要真是运行器
    except OSError:
        return False


def counts(w):
    c = {}
    for a in w.get("agents") or []:
        c[a["status"]] = c.get(a["status"], 0) + 1
    return c


def elapsed(w):
    end = w.get("finished") if w.get("status") in DONE else core.now()
    return (end or core.now()) - (w.get("started") or w.get("created") or core.now())


def row(w):
    o = w.get("origin") or {}
    c = counts(w)
    return {
        "id": w["id"], "name": w["name"], "status": w["status"], "owner": w.get("owner"), "session": o.get("session") or "",
        "agents": len(w.get("agents") or []), "counts": c, "tokens": sum(a.get("tokens", 0) for a in w.get("agents") or []),
        "elapsed": int(elapsed(w)), "started": w.get("started"), "finished": w.get("finished"),
        "origin": core.origin_label({"owner": w.get("owner"), "origin": {k: v for k, v in o.items() if k != "via"}}),
        "error": (w.get("error") or "")[:300],
    }


def step_of(tid):
    """运行中的代理正在干什么：最近一次工具调用 / 说的话，和距今多久。"""
    for e in reversed(core.read_events(tid, tail=25, max_bytes=65536)):
        k = e.get("k")
        if k == "tool":
            return f"{e.get('name')}: {e.get('arg', '')}", e.get("t")
        if k == "say":
            return "说：" + (e.get("text") or "").strip().replace("\n", " "), e.get("t")
        if k == "turn":
            return f"思考（第 {e.get('n')} 回合）", e.get("t")
        if k == "status" and e.get("to") == "queued":
            return "排队", e.get("t")
    return "", None


def tree(w, steps=0):
    """进度树：阶段 → 代理（状态、用时、回合、token、当前一步 / 结论）。steps > 0 时每个代理再带最近几步。"""
    now = core.now()
    agents = w.get("agents") or []
    order = [p["title"] for p in w.get("phases") or []]
    for a in agents:
        if a["phase"] not in order:
            order.append(a["phase"])
    detail = {p["title"]: p.get("detail", "") for p in w.get("phases") or []}
    phases = []
    for title in order:
        al = [a for a in agents if a["phase"] == title]
        rows = []
        for a in al:
            t0 = a.get("t_start") or a.get("t_queue")
            end = a.get("t_end") or (now if a["status"] not in ("done", "failed", "cancelled") else t0)
            r = {k: a.get(k) for k in ("n", "label", "status", "task", "cached", "retries", "turns", "tokens", "summary", "note", "session", "profile", "model")}
            r["elapsed"] = int((end or now) - (t0 or now))
            r["wait"] = int((a.get("t_start") or now) - a["t_queue"]) if a.get("t_queue") and a["status"] == "queued" else 0
            r["cont"] = bool(a.get("cont"))
            if a["status"] in ("running", "queued", "retry", "paused") and a.get("task"):
                s, t = step_of(a["task"])
                r["step"], r["step_ago"] = s, int(now - t) if t else None
            if steps and a.get("task"):
                r["recent"] = recent_steps(a["task"], steps)
            rows.append(r)
        done = sum(1 for a in al if a["status"] in ("done", "failed", "cancelled"))
        starts = [a.get("t_queue") for a in al if a.get("t_queue")]
        ends = [a.get("t_end") for a in al if a.get("t_end")]
        st = ("pending" if not al else "done" if done == len(al) else "running")
        ph_el = int(((max(ends) if st == "done" and ends else now) - min(starts))) if starts else 0
        phases.append({"title": title or "（未分阶段）", "detail": detail.get(title, ""), "status": st, "done": done, "total": len(al),
                       "failed": sum(1 for a in al if a["status"] == "failed"), "elapsed": ph_el, "agents": rows})
    return {"wf": row(w), "description": w.get("description", ""), "phases": phases, "logs": (w.get("logs") or [])[-30:],
            "result_preview": w.get("result_preview"), "error": w.get("error"), "attempt": w.get("attempt", 1), "args": w.get("args"),
            "cwd": w.get("cwd")}


def recent_steps(tid, n):
    out = []
    for e in core.read_events(tid, tail=80, max_bytes=131072):
        k = e.get("k")
        if k == "tool":
            out.append(f"{core.stamp(e['t'])} {e.get('name')}: {e.get('arg', '')}")
        elif k == "say":
            out.append(f"{core.stamp(e['t'])} 说：{(e.get('text') or '').strip()}")
        elif k == "submit":
            out.append(f"{core.stamp(e['t'])} 提交：{(e.get('summary') or '').strip()}")
        elif k == "steer":
            out.append(f"{core.stamp(e['t'])} 收到：{(e.get('text') or '').strip()}")
    return [core.clip(x, 220) for x in out[-n:]]


def render(tr, width=110):
    """进度树的文字版（crew wf show / watch，Claude 的 workflow show）。"""
    w = tr["wf"]
    c = w["counts"]
    cnt = " · ".join(f"{AZH.get(k, k)} {v}" for k, v in c.items())
    out = [f"{w['id']}「{w['name']}」 {ZH.get(w['status'], w['status'])}  {core.fmt_secs(w['elapsed'])}  代理 {w['agents']}（{cnt or '还没有'}）  {core.fmt_tokens(w['tokens'])} tok"
           + (f"  第 {tr['attempt']} 次启动" if tr.get("attempt", 1) > 1 else "")]
    if tr.get("description"):
        out.append("  " + core.clip(tr["description"], width - 2))
    for p in tr["phases"]:
        g = {"done": "✓", "running": "▶", "pending": "·"}[p["status"]]
        out.append(f"{g} {p['title']}  {p['done']}/{p['total']}" + (f"  失败 {p['failed']}" if p["failed"] else "")
                   + (f"  {core.fmt_secs(p['elapsed'])}" if p["total"] else "") + (f"  — {p['detail']}" if p["detail"] else ""))
        for a in p["agents"]:
            head = (f"   {GLYPH.get(a['status'], ' ')} {core.pad(str(a['n']), 4)}{core.pad(a['label'], 30)} "
                    f"{core.pad(AZH.get(a['status'], a['status']), 5)}{core.pad(core.fmt_secs(a['elapsed']), 7)}"
                    f"{core.pad(str(a['turns'] or 0) + '回合', 7)}{core.pad(core.fmt_tokens(a['tokens']), 6)}")
            tag = (f"[{a['model']}] " if a.get("model") else "") + ("缓存 " if a["cached"] else "") + ("续上文 " if a.get("cont") else "") + (f"重试{a['retries']} " if a["retries"] else "")
            if a["status"] in ("running", "queued", "retry", "paused"):
                tail = a.get("step") or ""
                if a.get("step_ago") is not None:
                    tail += f"（{core.fmt_secs(a['step_ago'])}前）"
            elif a["status"] == "failed":
                tail = "✗ " + (a.get("note") or "")
            elif a["status"] == "cancelled":
                tail = "已取消"
            else:
                tail = "→ " + (a.get("summary") or "")
            out.append(core.clip(head + tag + tail, width))
            for s in a.get("recent") or []:
                out.append(core.clip("        " + s, width))
    if tr.get("logs"):
        out.append("日志：")
        out += [core.clip(f"  {core.stamp(l['t'])} {l['msg']}", width) for l in tr["logs"][-8:]]
    if tr.get("error"):
        out.append("错误：" + core.clip(tr["error"].splitlines()[0], width))
    if tr.get("result_preview") and w["status"] == "done":
        out.append("返回值（预览）：" + core.clip(tr["result_preview"], width * 3))
    return "\n".join(out)


def digest(w):
    """工作流结束时交给 Claude 的凝练结果。"""
    r = row(w)
    c = r["counts"]
    lines = [f"工作流 {w['id']}「{w['name']}」{ZH.get(w['status'])} · 用时 {core.fmt_secs(r['elapsed'])} · 代理 {r['agents']} 个"
             f"（完成 {c.get('done', 0)}，失败 {c.get('failed', 0)}）· {core.fmt_tokens(r['tokens'])} tok"]
    fails = [a for a in w.get("agents") or [] if a["status"] == "failed"]
    if fails:
        lines.append("失败的代理（返回了 null）：" + "；".join(f"{a['n']}「{a['label']}」{a.get('task')}：{core.clip(a.get('note') or '', 80)}" for a in fails[:8]))
    if w.get("error"):
        lines.append("脚本出错：" + core.clip(w["error"], 800))
    res = core.read_json(os.path.join(wdir(w["id"]), "result.json"))
    if res is not None:
        s = json.dumps(res.get("value"), ensure_ascii=False, indent=1)
        lines.append("返回值：" + (s if len(s) <= 3000 else s[:3000] + f"…（共 {len(s)} 字，全文：crew wf result {w['id']}）"))
    return "\n".join(lines)


# ───────── 后台服务调用
class Host:
    """后台服务里管运行器进程：拉起、续跑、回收。"""

    def __init__(self, log):
        self.log = log
        self.procs = {}
        self.crashes = {}
        self.ended = set()       # 已结束的工作流：不再每轮读它们的状态文件，除非有人请求续跑 / 取消

    def spawn(self, wid, why):
        d = wdir(wid)
        env = {**os.environ, "CREW_PY": sys.executable, "CREW_HOME": core.HOME}
        try:
            with open(os.path.join(d, "runner.log"), "a") as lf:
                p = subprocess.Popen([node(), RUNNER, "run", d], stdout=lf, stderr=subprocess.STDOUT, start_new_session=True, env=env, cwd=d)
        except OSError as e:
            st = core.read_json(os.path.join(d, "state.json")) or {}
            st.update(status="failed", error=f"运行器起不来：{e}", finished=core.now())
            core.write_json(os.path.join(d, "state.json"), st)
            self.log(f"工作流 {wid} 运行器起不来：{e}")
            return
        self.procs[wid] = p
        core.write_json(os.path.join(d, "proc.json"), {"pid": p.pid, "host": core.host_id(), "t": core.now()})
        self.log(f"工作流 {wid} 运行器启动（{why}）pid {p.pid}")

    def scan(self):
        for wid, p in list(self.procs.items()):
            if p.poll() is not None:
                self.procs.pop(wid)
        for wid in list_ids():
            d = wdir(wid)
            start = os.path.join(d, "ctl", "start")
            cancel = os.path.join(d, "ctl", "cancel")
            if wid in self.ended and not (os.path.exists(start) or os.path.exists(cancel)):
                continue
            proc = core.read_json(os.path.join(d, "proc.json")) or {}
            here = proc.get("host") in (None, core.host_id())
            alive = here and (wid in self.procs or pid_alive(proc.get("pid")))
            if os.path.exists(cancel) and not alive:
                # 取消时运行器不在：在这里收尾（运行器在的话它自己收尾），不再拉起
                self.finish_cancel(wid)
                continue
            if os.path.exists(start):
                self.ended.discard(wid)
                if not alive:
                    self.spawn(wid, "开始" if not proc else "续跑")
                os.remove(start)
                continue
            st = core.read_json(os.path.join(d, "state.json")) or {}
            if st.get("status") in DONE and not alive:
                self.ended.add(wid)
                continue
            if st.get("status") == "running" and here and not alive:
                n = [t for t in self.crashes.get(wid, []) if core.now() - t < 600] + [core.now()]
                self.crashes[wid] = n
                if len(n) > 5:
                    st.update(status="failed", error="运行器 10 分钟内退出了 5 次以上，停止自动续跑；看 runner.log，修好后 crew wf resume", finished=core.now())
                    core.write_json(os.path.join(d, "state.json"), st)
                    self.log(f"工作流 {wid} 运行器反复退出，停止续跑")
                else:
                    self.spawn(wid, "运行器不在了，续跑")

    def finish_cancel(self, wid):
        d = wdir(wid)
        st = core.read_json(os.path.join(d, "state.json")) or {}
        for ag in st.get("agents") or []:
            if ag.get("task") and ag.get("status") not in ("done", "failed", "cancelled"):
                core.drop(ag["task"], "cancel", frm="工作流")
                ag.update(status="cancelled", t_end=core.now())
        if st.get("status") not in DONE:
            st.update(status="cancelled", finished=core.now())
            st.setdefault("logs", []).append({"t": core.now(), "msg": "工作流被取消"})
            core.write_json(os.path.join(d, "state.json"), st)
        for n in ("cancel", "start"):
            try:
                os.remove(os.path.join(d, "ctl", n))
            except OSError:
                pass
        self.ended.add(wid)
        self.log(f"工作流 {wid} 已取消（运行器不在，由后台服务收尾）")


# ───────── 命令行
def _ensure():
    from .cli import ensure_up
    ensure_up()


def cmd(a):
    from .cli import out
    act = a.wf_cmd
    if act == "run":
        if a.stdin:
            spec = json.load(sys.stdin)
        else:
            spec = {"script_path": a.script, "args": json.loads(a.args) if a.args else None, "cwd": a.cwd, "profile": a.profile,
                    "model": a.model, "effort": a.effort, "concurrency": a.concurrency}
        run = create(spec, a.owner)
        _ensure()
        msg = {"id": run["id"], "name": run["name"], "phases": [p["title"] for p in run["phases"]]}
        out(msg, True) if a.json else print(f"{run['id']}  「{run['name']}」已交给 crew 运行；看进度：crew wf watch {run['id']}")
    elif act == "check":
        out(check(a.script), True)
    elif act == "ls":
        ws = [load(i) for i in list_ids()]
        ws = [w for w in ws if w]
        if a.owner:
            ws = [w for w in ws if w.get("owner") == a.owner]
        if not a.all:
            ws = [w for w in ws if w["status"] not in DONE or core.now() - (w.get("finished") or 0) < 86400]
        rows = [row(w) for w in ws]
        if a.json:
            out(rows, True)
            return
        if not rows:
            print("没有工作流")
        for r in rows:
            c = r["counts"]
            print(f"{core.pad(r['id'], 20)}{core.pad(ZH.get(r['status'], r['status']), 6)}{core.pad(core.fmt_secs(r['elapsed']), 8)}"
                  f"{core.pad(str(c.get('done', 0)) + '/' + str(r['agents']), 8)}{core.pad(core.fmt_tokens(r['tokens']), 7)}{r['name']}〔{r['origin']}〕")
    elif act in ("show", "watch"):
        wid = resolve(a.id)
        while True:
            w = load(wid)
            tr = tree(w, steps=a.verbose)
            if a.json:
                out(tr, True)
                return
            width = shutil.get_terminal_size((120, 40)).columns
            text = render(tr, width)
            if act == "show":
                print(text)
                return
            sys.stdout.write("\x1b[H\x1b[2J" + text + f"\n\n（每 2 秒刷新，Ctrl-C 退出；-v N 显示每个代理最近 N 步）\n")
            sys.stdout.flush()
            if w["status"] in DONE:
                return
            try:
                time.sleep(2)
            except KeyboardInterrupt:
                return
    elif act == "result":
        w = load(resolve(a.id))
        if a.full:
            res = core.read_json(os.path.join(wdir(w["id"]), "result.json"))
            print(json.dumps(res.get("value") if res else None, ensure_ascii=False, indent=1))
        else:
            print(digest(w))
    elif act == "cancel":
        wid = resolve(a.id)
        w = load(wid)
        if w["status"] in DONE:
            print(f"{wid} 已经结束（{ZH.get(w['status'])}）")
            return
        # 只放取消信号：运行器在就自己收尾；不在由后台服务收尾（同一处判断，不会和“续跑”抢着改状态）
        open(os.path.join(wdir(wid), "ctl", "cancel"), "w").close()
        _ensure()
        for _ in range(40):
            if not os.path.exists(os.path.join(wdir(wid), "ctl", "cancel")):
                break
            time.sleep(0.25)
        print(f"已取消 {wid}（在跑的代理任务一并取消）")
    elif act == "resume":
        wid = resolve(a.id)
        w = load(wid)
        if pid_alive((w.get("proc") or {}).get("pid")):
            print(f"{wid} 正在运行")
            return
        open(os.path.join(wdir(wid), "ctl", "start"), "w").close()
        _ensure()
        print(f"{wid} 将续跑：已完成的代理直接用结果，没做完的接着做")
    elif act == "rm":
        wid = resolve(a.id)
        w = load(wid)
        if w["status"] not in DONE and pid_alive((w.get("proc") or {}).get("pid")):
            raise SystemExit(f"{wid} 还在运行，先 crew wf cancel")
        shutil.rmtree(wdir(wid))
        print(f"已删除 {wid}（代理任务留着，crew rm 另删）")


def add_parser(sub):
    p = sub.add_parser("wf", help="工作流：run / ls / show / watch / result / cancel / resume / rm")
    ws = p.add_subparsers(dest="wf_cmd", required=True)
    s = ws.add_parser("run", help="运行工作流脚本（或 --stdin 传 {script|script_path, args, cwd, profile, origin}）")
    s.add_argument("script", nargs="?")
    s.add_argument("--args", help="JSON")
    s.add_argument("--cwd")
    s.add_argument("--profile")
    s.add_argument("--model", help="整个工作流的默认模型：flash / haiku / sonnet / opus / fable")
    s.add_argument("--effort", help="整个工作流的默认思考档位：none / low / medium / high / xhigh / max")
    s.add_argument("--concurrency", type=int, help="这个工作流里同时跑几个 agent()（0 = 不限；默认取脚本 meta.concurrency，再取配置 wf_concurrency）")
    s.add_argument("--stdin", action="store_true")
    s.add_argument("--owner", default="cli")
    s = ws.add_parser("check", help="只检查脚本格式")
    s.add_argument("script")
    s = ws.add_parser("ls", help="列出工作流")
    s.add_argument("--all", action="store_true")
    s.add_argument("--owner")
    for name in ("show", "watch"):
        s = ws.add_parser(name, help="进度树" if name == "show" else "实时进度（每 2 秒刷新）")
        s.add_argument("id")
        s.add_argument("-v", "--verbose", type=int, default=0, help="每个代理再显示最近 N 步")
    s = ws.add_parser("result", help="结果（默认凝练版；--full 完整返回值）")
    s.add_argument("id")
    s.add_argument("--full", action="store_true")
    for name in ("cancel", "resume", "rm"):
        s = ws.add_parser(name)
        s.add_argument("id")
    p.set_defaults(f=cmd)
