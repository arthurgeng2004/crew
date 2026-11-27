"""crew 后台服务：唯一写 task.json 的进程。调度排队任务、消费投递箱、维护心跳。"""
import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
import traceback

from . import core
from .worker import Worker
from . import wf


class Daemon:
    def __init__(self):
        core.ensure_dirs()
        self.cfg = core.config()
        self.tasks = {}          # id -> task dict（内存里的权威副本）
        self.workers = {}        # id -> Worker
        self.unpause = set()     # 正在暂停收尾时又被叫“继续”的任务：收尾后直接排队
        self.rl = {}             # 档案 -> {"bad": 最近一次 429, "ok": 最近一次成功}：限流时排队任务改派 fallback_profile
        self.probe_next = {}     # 巡检名 -> 下次跑的时间
        self.probing = set()     # 正在跑的巡检（各在自己的线程里，不卡主循环）
        self.lock = threading.RLock()
        self.stop = False
        self.started = core.now()
        self.log_path = os.path.join(core.HOME, "daemon.log")
        self.wf = wf.Host(self.log)

    def log(self, msg):
        try:
            if os.path.getsize(self.log_path) > 20_000_000:
                os.replace(self.log_path, self.log_path + ".1")      # 只留一份旧的
        except OSError:
            pass
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%F %T')}] {msg}\n")

    # ───────── 任务状态（只在这里写 task.json）
    def update_task(self, tid, **kw):
        with self.lock:
            t = self.tasks.get(tid) or core.load_task(tid)
            if not t:
                return None
            t.update(kw)
            t["updated"] = core.now()
            self.tasks[tid] = t
            core.write_json(os.path.join(core.task_dir(tid), "task.json"), t)
            return t

    def set_status(self, tid, status, note="", **kw):
        t = self.tasks.get(tid) or {}
        old = t.get("status")
        extra = {}
        if status == "running" and not t.get("started"):
            extra["started"] = core.now()
        if status in core.FINAL or status in ("review", "failed"):
            extra["finished"] = core.now()
        elif status in ("running", "queued"):
            extra["finished"] = None
        if status in core.NEEDS_LEAD:
            extra["needs_since"] = core.now()
        self.update_task(tid, status=status, note=note, **extra, **kw)
        if old != status:
            core.append_event(tid, "status", frm=old, to=status, note=note[:500])

    def worker_done(self, tid, status, note):
        with self.lock:
            w = self.workers.pop(tid, None)
            # 工人收尾时还在路上的插话 / 回答：在锁里收进 pending.json，下次续跑时读到（不会丢进没人读的队列）
            left = w.take_queue() if w else []
            if left:
                self.to_pending(tid, *left)
            if status == "switch":
                if self.switch_profile(tid, note or "限流"):
                    self.set_status(tid, "queued", note or "限流，换档案续做")
                    return
                status = "failed"
            if status == "paused" and (self.stop or tid in self.unpause):
                # 服务停止时被打断的，或暂停途中又被叫继续的：排队接着跑
                self.unpause.discard(tid)
                self.set_status(tid, "queued", "后台服务重启，排队续跑" if self.stop else "继续")
                return
            t = self.tasks.get(tid) or {}
            if status == "review" and t.get("wf"):
                # 工作流里的任务：成果由脚本直接收下，不叫醒 Claude 验收
                self.set_status(tid, "accepted", "工作流收下")
            else:
                self.set_status(tid, status, note)
            self.log(f"{tid} → {(self.tasks.get(tid) or {}).get('status', status)} {note[:200]}")

    # ───────── 限流改派：档案写了 fallback_profile 才改派（默认不写）
    def note_rate(self, prof, ok):
        r = self.rl.setdefault(prof, {"bad": 0, "ok": 0})
        r["ok" if ok else "bad"] = core.now()

    def limited(self, prof):
        r = self.rl.get(prof) or {}
        return r.get("bad", 0) > r.get("ok", 0) and core.now() - r.get("bad", 0) < 300

    def switch_profile(self, tid, why):
        """把任务换到档案的 fallback_profile：chat 对话记录改名留作交接（新档案开新 Codex 会话），排队续做。"""
        t = self.tasks.get(tid) or {}
        old = t.get("profile") or self.cfg.get("default_profile") or "gpt"
        fb = (self.cfg["profiles"].get(old) or {}).get("fallback_profile")
        if not fb or fb not in self.cfg["profiles"]:
            return False
        d = core.task_dir(tid)
        sw = {"from": old, "to": fb, "t": core.now(), "why": why}
        src = os.path.join(d, "msgs.json")
        if os.path.exists(src):
            sw["msgs"] = f"msgs.{old}.json"
            os.replace(src, os.path.join(d, sw["msgs"]))
        self.update_task(tid, profile=fb, model=None, switched=sw)
        core.append_event(tid, "model", text=f"{why}：改用 {fb}" + ("（交接已做的部分）" if sw.get("msgs") else ""))
        self.log(f"{tid} {why}，改用 {fb}")
        return True

    def retire_profile(self, tid):
        """任务的档案已不存在：改到 default_profile，chat 对话记录改名交接。"""
        t = self.tasks.get(tid) or {}
        old = t.get("profile")
        new = core.profile_name(old, self.cfg)
        d = core.task_dir(tid)
        sw = {"from": old, "to": new, "t": core.now(), "why": f"{old} 档案不存在"}
        src = os.path.join(d, "msgs.json")
        if os.path.exists(src):
            sw["msgs"] = f"msgs.{old}.json"
            os.replace(src, os.path.join(d, sw["msgs"]))
        self.update_task(tid, profile=new, model=None, switched=sw)
        core.append_event(tid, "model", text=f"{old} 档案不存在：改用 {new}" + ("（交接已做的部分）" if sw.get("msgs") else ""))
        self.log(f"{tid} {old} 档案不存在，改用 {new}")

    # ───────── 投递箱
    def handle_inbox(self, tid):
        d = os.path.join(core.task_dir(tid), "inbox")
        try:
            names = sorted(os.listdir(d))
        except FileNotFoundError:
            return
        for n in names:
            if not n.endswith(".json"):
                continue
            p = os.path.join(d, n)
            item = core.read_json(p)
            try:
                os.remove(p)
            except OSError:
                pass
            if item:
                try:
                    self.apply(tid, item)
                except Exception as e:  # noqa: BLE001
                    core.append_event(tid, "error", text=f"处理投递 {item.get('kind')} 失败：{e}")
                    self.log(traceback.format_exc())

    def to_pending(self, tid, *items):
        p = os.path.join(core.task_dir(tid), "pending.json")
        core.write_json(p, core.read_json(p, []) + list(items))

    def apply(self, tid, it):
        t = self.tasks.get(tid)
        if not t:
            return
        k = it.get("kind")
        w = self.workers.get(tid)
        st = t.get("status")
        who = it.get("from") or "组长"
        if k in ("steer", "answer"):
            core.append_event(tid, "steer" if k == "steer" else "answer", text=it.get("text", "")[:1000], frm=who)
            if w:
                w.q.put(it)
            else:
                self.to_pending(tid, it)
                if st == "waiting":
                    self.set_status(tid, "queued", f"{who}已回答", question=None)
                # 待验收 / 失败时的插话：留在 pending.json，下次修改或续跑时一并读到
        elif k == "followup":
            # 接着同一个工人的上下文做下一步：任务已交过成果（待验收 / 已通过）才能接
            if st not in ("review", "accepted", "failed"):
                core.append_event(tid, "error", text=f"任务状态是 {st}，还没交成果，不能接下一步（要插话用 say）")
                return
            rnd = int(t.get("round") or 1) + 1
            core.append_event(tid, "steer", text="下一步：" + it.get("text", "")[:1000], frm=who)
            self.to_pending(tid, {"kind": "followup", "text": it.get("text", ""), "round": rnd, "from": who})
            # 轮次和状态一次写进 task.json：别人读不到“已通过但轮次已加一”的中间状态
            self.set_status(tid, "queued", f"{who}派下一步（第 {rnd} 步）", round=rnd, **({"schema": it["schema"]} if "schema" in it else {}))
        elif k == "pause":
            self.unpause.discard(tid)
            if w:
                w.interrupt("paused", f"{who}暂停")
            elif st == "queued":
                self.set_status(tid, "paused", f"{who}暂停")
        elif k == "resume":
            if w and w.want == "paused":
                self.unpause.add(tid)       # 还在暂停收尾：收尾后直接排队
            elif st == "paused" or (st == "failed" and it.get("retry")):
                self.set_status(tid, "queued", f"{who}继续")
        elif k == "cancel":
            if w:
                w.interrupt("cancelled", f"{who}取消")
            elif st not in core.FINAL:
                self.set_status(tid, "cancelled", f"{who}取消")
        elif k == "review":
            v = it.get("verdict")
            rec = {"verdict": v, "score": it.get("score"), "text": it.get("text", ""), "by": who, "t": core.now(), "round": t.get("round", 1)}
            reviews = (t.get("reviews") or []) + [rec]
            core.append_event(tid, "review", verdict=v, score=it.get("score"), text=it.get("text", "")[:1500], frm=who)
            if st not in ("review", "failed") and v != "reject":
                core.append_event(tid, "error", text=f"任务状态是 {st}，不能验收为 {v}")
                return
            if v == "accept":
                self.set_status(tid, "accepted", f"{who}验收通过", reviews=reviews)
            elif v == "reject":
                if w:
                    self.update_task(tid, reviews=reviews)
                    w.interrupt("rejected", f"{who}否决")     # 工人收尾时落成“否决”
                else:
                    self.set_status(tid, "rejected", f"{who}否决", reviews=reviews)
            elif v == "revise":
                rnd = int(t.get("round") or 1) + 1
                self.to_pending(tid, {"kind": "revise", "text": it.get("text", ""), "score": it.get("score"), "round": rnd})
                self.set_status(tid, "queued", f"{who}退回修改（第 {rnd} 轮）", reviews=reviews, round=rnd)

    # ───────── 主循环
    def scan(self):
        for name in os.listdir(core.TASKS):
            if name not in self.tasks:
                t = core.load_task(name)
                if t:
                    self.tasks[name] = t
        for tid, t in list(self.tasks.items()):
            if not os.path.isdir(core.task_dir(tid)):
                self.tasks.pop(tid)
                continue
            self.handle_inbox(tid)
        running = sum(1 for w in list(self.workers.values()) if w.is_alive())
        cap = int(self.cfg.get("max_parallel") or 0) or float("inf")     # 0 = 不限
        # 按对话轮流：先看优先级，再挑正在跑的任务最少的那个对话，同一对话里先派先跑。
        # 一个对话一次派一大批，也不会把别的对话的任务压在后面。
        load = {}
        for tid, w in self.workers.items():
            if w.is_alive() and tid in self.tasks:
                load[group(self.tasks[tid])] = load.get(group(self.tasks[tid]), 0) + 1
        # 按档案限并发（档案的 max_parallel，如 GPT 网关只给 2 个名额）：满了的档案，它的任务留在排队里
        # 档案可写 parallel_group：同组的档案共用名额（gpt 与 gpt-chat 都打同一个网关）
        pname = lambda x: core.profile_name(x.get("profile"), self.cfg)
        pdef = lambda x: self.cfg["profiles"].get(pname(x)) or {}
        prof_of = lambda x: pdef(x).get("parallel_group") or pname(x)
        pcap_of = lambda x: int(pdef(x).get("max_parallel") or 0) or float("inf")
        busy = {}
        for tid, w in self.workers.items():
            if w.is_alive() and tid in self.tasks:
                busy[prof_of(self.tasks[tid])] = busy.get(prof_of(self.tasks[tid]), 0) + 1
        queued = [t for tid, t in self.tasks.items() if t.get("status") == "queued" and tid not in self.workers]
        # 档案已不存在的排队任务：换到 default_profile，开过工的 chat 记录改名留作交接，和限流改派同一套
        for x in queued:
            if x.get("profile") and x["profile"] not in self.cfg["profiles"] and core.profile_name(x["profile"], self.cfg) in self.cfg["profiles"]:
                self.retire_profile(x["id"])
        # 档案正被限流、又有 fallback_profile 的：还没开工的排队任务直接改派（开过工的有对话记录，同样交接）
        for x in queued:
            if pdef(x).get("fallback_profile") and x.get("fallback") is not False and self.limited(pname(x)):
                self.switch_profile(x["id"], f"{pname(x)} 正被限流")
        # Claude 档案：周额度用到 quota_stop 就不开新任务（排队等重置），排队的任务上写明原因
        from .clauderun import quota_held
        held = {}
        for x in queued:
            why = quota_held(pdef(x)) if pdef(x).get("backend") == "claude" else ""
            if why:
                held[x["id"]] = why
                if x.get("note") != why:
                    self.update_task(x["id"], note=why)
        queued = [x for x in queued if x["id"] not in held]
        while running < cap and queued:
            ok = [x for x in queued if busy.get(prof_of(x), 0) < pcap_of(x)]
            if not ok:
                break
            t = min(ok, key=lambda x: (-(x.get("priority") or 0), load.get(group(x), 0), x.get("created", 0)))
            queued.remove(t)
            busy[prof_of(t)] = busy.get(prof_of(t), 0) + 1
            tid = t["id"]
            self.set_status(tid, "running", "开始" if not t.get("started") else "续跑")
            w = Worker(self, self.tasks[tid])
            self.workers[tid] = w
            w.start()
            load[group(t)] = load.get(group(t), 0) + 1
            running += 1

    # ───────── 巡检：定期确认数据源活着；坏了开告警（叫醒用到它的 Claude 对话），好了解除（再叫醒说恢复了）
    def probes(self):
        for p in self.cfg.get("probes") or []:
            name = p.get("name") or p.get("cmd")
            if name in self.probing or core.now() < self.probe_next.get(name, 0):
                continue
            self.probing.add(name)
            threading.Thread(target=self.run_probe, args=(p, name), daemon=True).start()

    def run_probe(self, p, name):
        ok, out = False, ""
        try:
            r = subprocess.run(["bash", "-lc", p["cmd"]], capture_output=True, text=True, timeout=float(p.get("timeout_s") or 120), stdin=subprocess.DEVNULL)
            ok, out = r.returncode == 0, (r.stdout + r.stderr)[-3000:]
        except subprocess.TimeoutExpired:
            out = f"巡检超时（{p.get('timeout_s') or 120} 秒没有返回）"
        except Exception as e:  # noqa: BLE001
            out = f"巡检跑不起来：{e}"
        try:
            if ok:
                done = core.resolve_alerts(p.get("match") or [name], f"巡检「{name}」恢复正常")
                if done:
                    self.log(f"巡检「{name}」恢复正常，解除告警：{', '.join(done)}")
            else:
                hits = core.scan_alerts(out, self.cfg)
                key, label, host, line = hits[0] if hits else ("巡检:" + name, f"{name} 巡检失败", "", out.strip().splitlines()[-1] if out.strip() else "")
                a = core.raise_alert(key, label, host, line, "巡检", self.cfg)
                if a["count"] == 1:
                    self.log(f"巡检「{name}」失败，开告警 {key}：{line[:200]}")
        finally:
            self.probe_next[name] = core.now() + float(p.get("every_s" if ok else "every_fail_s") or p.get("every_s") or 600)
            self.probing.discard(name)

    def heartbeat(self):
        core.write_json(os.path.join(core.HOME, "daemon.json"), {
            "pid": os.getpid(), "host": core.host_id(), "started": self.started, "beat": core.now(),
            "running": len(self.workers), "max_parallel": self.cfg.get("max_parallel"),
        })

    def recover(self):
        """上次没收尾的任务：运行中的改回排队续跑。"""
        for t in core.list_tasks():
            self.tasks[t["id"]] = t
            if t.get("status") == "running":
                self.set_status(t["id"], "queued", "后台服务重启，排队续跑")

    def run(self):
        # 单实例锁：只要旧的后台服务进程还活着（哪怕正在收尾、心跳停了），新的就起不来，杜绝两个服务抢着处理同一批任务。
        # 锁跟着打开的文件走；原地重启（execv）时文件关闭、锁释放，新映像马上重新拿到。
        self.lockf = open(os.path.join(core.HOME, "daemon.lock"), "w")
        try:
            fcntl.flock(self.lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("已有后台服务在运行（单实例锁被占用）", file=sys.stderr)
            sys.exit(1)
        info = core.daemon_info()
        if info and info["alive"] and info.get("pid") != os.getpid():
            print(f"已有后台服务在运行（{info['host']} pid {info['pid']}）", file=sys.stderr)
            sys.exit(1)
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop", True))
        self.heartbeat()
        self.recover()
        self.log(f"启动 pid {os.getpid()} 于 {core.host_id()}，并行 {self.cfg.get('max_parallel') or '不限'}")
        last_cfg = 0
        born = code_mtime()
        while not self.stop:
            try:
                if core.now() - last_cfg > 10:
                    self.cfg = core.config()
                    last_cfg = core.now()
                    # crew 的代码改了：等手上没有在跑的工人时自己换成新代码（同一个 pid 重新执行）；急用就 crew restart
                    if code_mtime() > born and not any(w.is_alive() for w in list(self.workers.values())):
                        bad = code_errors()
                        if bad:
                            self.log(f"代码已更新但有语法错误，先不重启：{bad}")
                            born = code_mtime()
                        else:
                            self.log("代码已更新，空闲时自动重启换成新代码")
                            os.execv(sys.executable, [sys.executable, "-m", "crewlib.daemon"])
                if os.path.exists(os.path.join(core.CTL, "stop")):
                    os.remove(os.path.join(core.CTL, "stop"))
                    self.stop = True
                    break
                if os.path.exists(os.path.join(core.CTL, "restart")):
                    # crew restart：在这台机器上原地重启换新代码；运行中的任务打断后排队续跑
                    os.remove(os.path.join(core.CTL, "restart"))
                    self.stop = self.restart = True
                    break
                with self.lock:
                    self.scan()
                self.wf.scan()
                self.probes()
                self.heartbeat()
            except Exception:  # noqa: BLE001
                self.log(traceback.format_exc())
            time.sleep(0.5)
        self.log("停止中：打断运行中的任务，下次启动续跑")
        for w in list(self.workers.values()):
            w.interrupt("paused")
        # 等工人收尾时照常写心跳：别的会话看到心跳停了会以为服务挂了、再起一个
        deadline = time.time() + 30
        while any(w.is_alive() for w in list(self.workers.values())) and time.time() < deadline:
            self.heartbeat()
            time.sleep(0.5)
        if getattr(self, "restart", False):
            bad = code_errors()
            if not bad:
                # 不删 daemon.json：心跳还新，重启这一两秒里别的会话不会以为服务挂了去再起一个
                self.log("原地重启")
                os.execv(sys.executable, [sys.executable, "-m", "crewlib.daemon"])
            self.log(f"新代码有错误，不重启、直接停止：{bad}")
        try:
            os.remove(os.path.join(core.HOME, "daemon.json"))
        except OSError:
            pass
        self.log("已停止")


def group(t):
    """排队轮流的单位：派发的对话；不是从对话派的按派发者。"""
    return (t.get("origin") or {}).get("session") or t.get("owner") or ""


def code_mtime():
    d = os.path.dirname(os.path.abspath(__file__))
    return max(os.path.getmtime(os.path.join(d, n)) for n in os.listdir(d) if n.endswith(".py"))


def code_errors():
    """新代码能不能用：改到一半的文件不能换上去，否则后台服务起不来。先查语法，再在子进程里真的导入一遍（查缺名字、缺模块）。"""
    d = os.path.dirname(os.path.abspath(__file__))
    for n in sorted(os.listdir(d)):
        if n.endswith(".py"):
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    compile(f.read(), n, "exec")
            except SyntaxError as e:
                return f"{n}:{e.lineno} {e.msg}"
    root = os.path.dirname(d)
    r = subprocess.run([sys.executable, "-c", "import crewlib.daemon, crewlib.cli, crewlib.tui"], cwd=root, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": root}, timeout=60)
    return "" if r.returncode == 0 else (r.stderr.strip().splitlines() or ["导入失败"])[-1][:300]


def main():
    Daemon().run()


if __name__ == "__main__":
    main()
