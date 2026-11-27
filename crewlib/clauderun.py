"""claude 后端：工人跑在本机 Claude Code 的无界面模式里（claude -p，stream-json 进出）。

默认不启用：只有派活时明确写 profile: "claude" 才走这里。
用的是本机登录的 Claude 账号额度，和组长对话共用同一份周额度：档案 max_parallel 限同时几个，
quota_stop 限周额度用到多少就不再开新的 Claude 任务（额度状态取自 Claude Code 每回合报的 rate_limit_event，
存 ~/.crew/claude_quota.json，后台服务排队时读）。

和 Codex 后端同一套外壳（继承 CodexRun）：开场消息、交接、插话 / 回答 / 退回意见的文字、submit_result 等 crew 工具的处理、
回合结束后的判定都一样。不同的地方：
- 进程：claude -p --input-format stream-json --output-format stream-json，组长的话作为 user 消息写进 stdin；
  回合进行中写进去的消息 Claude Code 会排进当前会话（当场插话）。暂停 / 取消发 control_request interrupt。
- crew 工具：用 --mcp-config 挂一个 stdio MCP 服务（crewlib/claudemcp.py，服务名 crewtask），它把调用经任务目录下的
  tools.sock 转回这里的 dyn_call。
- 续跑：任务目录 claude.json 记 session id，用 --resume 接上同一个 Claude 会话。
- 禁用：子代理 / 工作流（会绕过并发名额）和 crew 插件的派活工具（工人不能再派工人）；回测时禁用实时网页。
"""
import json
import os
import queue
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from . import core
from .codexrun import CodexRun

DEFAULT_BIN = "claude"
QUOTA = os.path.join(core.HOME, "claude_quota.json")
CREW_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CLAUDE_NOTE = """[crew 工人模式]
你跑在 Claude Code 的无界面模式里，Claude Code 的工具都能用（Bash、Read、Write、Edit、Grep、MCP 数据服务等、skills）。
另外有 crew 的工具（MCP 服务 crewtask）：mcp__crewtask__submit_result（完成时提交，恰好一次）、mcp__crewtask__report_problem（数据源 / 服务故障时报告）、mcp__crewtask__ask_lead（问组长，问完立刻结束本回合等回答）、mcp__crewtask__team_list / team_read / team_post（看队友、读队友的完整成果、给队友留言）。
- 不要启动子代理或工作流；不要等待用户确认——没有人在终端前，只有组长通过 crew 和你交流。
- MCP 工具结果很大时，用 Bash 里的 python 落盘再处理。
- python3 / pip 用 PATH 里的（crew/workerbin，带齐常用研究库）。"""

# 工人不能用的：子代理 / 工作流（绕过并发名额）、crew 插件的组长工具（工人不能再派工人、不能发跨对话消息）
DISALLOWED = ["Task", "Agent", "Workflow", "SendMessage", "ScheduleWakeup", "CronCreate", "RemoteTrigger", "PushNotification"] + [
    f"mcp__crew__{n}" for n in ("dispatch", "workflow", "steer", "review", "control", "message", "wait", "checkin", "alert", "status", "inspect")]


def save_quota(info):
    """rate_limit_event → ~/.crew/claude_quota.json（后台服务排队时据此决定开不开新的 Claude 任务）。"""
    try:
        win = info.get("unifiedWindows") or {}
        util = max([float(v.get("utilization") or 0) for v in win.values()] + [float(info.get("utilization") or 0)])
        core.write_json(QUOTA, {"utilization": util, "status": info.get("status"), "type": info.get("rateLimitType"),
                                "resets_at": info.get("resetsAt"), "t": core.now()})
    except (TypeError, ValueError, OSError):
        pass


def quota_held(prof):
    """档案写了 quota_stop（如 0.9）且最近记到的周额度用量 ≥ 它、还没到重置时间：不开新的 Claude 任务。返回原因或空串。"""
    stop = prof.get("quota_stop")
    if not stop:
        return ""
    q = core.read_json(QUOTA) or {}
    util, reset = float(q.get("utilization") or 0), float(q.get("resets_at") or 0)
    if reset and reset <= core.now():
        return ""
    if util >= float(stop) or q.get("status") == "rejected":
        when = time.strftime("%m-%d %H:%M UTC", time.gmtime(reset)) if reset else "未知"
        return f"Claude 额度已用 {util:.0%}（≥ {float(stop):.0%}），{when} 重置前不开新的 Claude 任务"
    return ""


class ClaudeRun(CodexRun):
    def __init__(self, w):
        super().__init__(w)
        self.state_path = os.path.join(w.dir, "claude.json")
        self.st = core.read_json(self.state_path) or {}
        self.proc = None
        self.inbox = queue.Queue()
        self.open = 0                 # 写进去、还没等到 result 的 user 消息数
        self.lock = threading.Lock()
        self.sock = None
        self.sock_path = None
        self.sock_dir = None

    def developer(self):
        note = self.cfg.get("worker_env_note") or ""
        return (self.t.get("system") or self.W.SYSTEM) + "\n\n" + CLAUDE_NOTE + ("\n\n" + note if note else "")

    def env(self):
        env = {**os.environ, **(self.cfg.get("worker_env") or {})}
        env.setdefault("CREW_PYTHON", sys.executable)
        env["PATH"] = self.W.WORKERBIN + os.pathsep + env.get("PATH", "")
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SSE_PORT"):
            env.pop(k, None)              # 后台服务可能是从某个 Claude 对话里拉起的：别让工人以为自己是它的子进程
        env["CREW_WORKER_TASK"] = self.w.tid
        return env

    # ───────── crew 工具：MCP 服务经 tools.sock 回调到这里
    def serve_tools(self):
        path = os.path.join(self.w.dir, "tools.sock")
        try:
            os.unlink(path)
        except OSError:
            pass
        if len(path.encode()) > 100:
            # macOS has no Linux abstract Unix sockets. Use a private short directory.
            base = tempfile.gettempdir()
            if len(os.fsencode(base)) > 60:
                base = "/tmp"
            self.sock_dir = tempfile.mkdtemp(prefix="crew-sock-", dir=base)
            path = os.path.join(self.sock_dir, "tools.sock")
        self.sock_path = path
        with open(os.path.join(self.w.dir, "claude-sock"), "w", encoding="utf-8") as f:
            f.write(path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        s.listen(8)
        self.sock = s

        def handle(c):
            try:
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = c.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.decode("utf-8") or "{}")
                with self.lock:
                    text, ok = self.dyn_call(req.get("name"), req.get("args") or {})
                c.sendall((json.dumps({"text": text, "ok": ok}, ensure_ascii=False) + "\n").encode("utf-8"))
            except Exception as e:     # noqa: BLE001 — 回调出错要回给工人，不能把工人线程带崩
                try:
                    c.sendall((json.dumps({"text": f"crew 处理出错：{e}", "ok": False}, ensure_ascii=False) + "\n").encode("utf-8"))
                except OSError:
                    pass
            finally:
                c.close()

        def loop():
            while True:
                try:
                    c, _ = s.accept()
                except OSError:
                    return
                threading.Thread(target=handle, args=(c,), daemon=True).start()

        threading.Thread(target=loop, daemon=True).start()

    # ───────── 启动
    def start(self):
        w = self.w
        mid, eff, alias = w.model_params()
        self.mid, self.eff = mid, eff
        tools = [{"name": x["name"], "description": x["description"], "inputSchema": x["inputSchema"]} for x in self.dyn_tools()]
        core.write_json(os.path.join(w.dir, "claude-tools.json"), tools)
        mcp_cfg = os.path.join(w.dir, "claude-mcp.json")
        core.write_json(mcp_cfg, {"mcpServers": {"crewtask": {
            "command": sys.executable, "args": ["-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); from crewlib import claudemcp; sys.argv = sys.argv[1:]; claudemcp.main()", CREW_ROOT, w.dir]}}})
        self.serve_tools()
        fresh = not self.st.get("session")
        sid = self.st.get("session") or str(uuid.uuid4())
        argv = [os.path.expanduser(self.prof.get("claude_bin") or DEFAULT_BIN), "-p",
                "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                *(["--model", mid] if mid else []), "--permission-mode", "bypassPermissions",
                "--mcp-config", mcp_cfg, "--append-system-prompt", self.developer()]
        argv += ["--session-id", sid] if fresh else ["--resume", sid]
        if eff:
            argv += ["--effort", eff]
        dis = list(DISALLOWED) + (["WebSearch", "WebFetch"] if self.t.get("cutoff") else [])   # 回测：只给 crew 的 web_fetch（archive.org）
        argv += ["--disallowedTools", ",".join(dis)]
        self.errlog = open(os.path.join(w.dir, "claude-stderr.log"), "ab")
        self.proc = subprocess.Popen(argv, cwd=self.t["cwd"], env=self.env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.errlog, start_new_session=True)

        def reader():
            for raw in self.proc.stdout:
                try:
                    self.inbox.put(json.loads(raw.decode("utf-8", "replace")))
                except ValueError:
                    continue
            self.inbox.put({"type": "__eof__"})

        threading.Thread(target=reader, daemon=True).start()
        if fresh:
            self.save_state(session=sid, model=mid)
            w.update(model_used=f"{mid} · {eff or '默认'} · Claude", claude_session=sid)
            w.ev("model", text=f"{mid} · {eff or '默认'}（Claude 会话 {sid[:8]}）")
        return fresh

    def write(self, o):
        try:
            self.proc.stdin.write((json.dumps(o, ensure_ascii=False) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise RuntimeError(f"Claude 进程收不到消息：{e}；{self.tail_log()}")

    def start_turn(self, texts):
        if not self.open:
            self.submitted = self.asked = None
        self.write({"type": "user", "message": {"role": "user", "content": "\n\n".join(texts)}})
        self.open += 1
        self.turn = "claude"
        if getattr(self, "clear_pending", False):
            core.write_json(os.path.join(self.w.dir, "pending.json"), [])
            self.clear_pending = False

    def interrupt(self):
        if self.proc and self.proc.poll() is None and self.open:
            try:
                self.write({"type": "control_request", "request_id": f"crew-{uuid.uuid4().hex[:8]}", "request": {"subtype": "interrupt"}})
            except RuntimeError:
                pass

    def tail_log(self):
        try:
            with open(os.path.join(self.w.dir, "claude-stderr.log"), "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 600))
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    # ───────── 主循环
    def run(self):
        w = self.w
        try:
            fresh = self.start()
            texts = self.take_inputs(True)
            if fresh:
                texts = [self.first_message()] + texts
            elif not texts:
                texts = ["[crew] 后台服务重启后续跑：接着之前的工作做，完成后调用 submit_result。"]
            self.start_turn(texts)
            nudges = fails = 0
            while True:
                if w.want:
                    self.interrupt()
                    raise w.stop_now()
                more = self.take_inputs(False)
                if more:
                    self.start_turn(more)          # 回合进行中写进去 = 当场插话；回合之间 = 新一回合
                try:
                    o = self.inbox.get(timeout=0.5)
                except queue.Empty:
                    continue
                k = o.get("type")
                if k == "__eof__":
                    raise RuntimeError("Claude 进程意外退出：" + self.tail_log())
                if k == "assistant":
                    self.on_assistant(o.get("message") or {})
                elif k == "user":
                    self.on_tool_results(o.get("message") or {})
                elif k == "rate_limit_event":
                    save_quota(o.get("rate_limit_info") or {})
                elif k == "system" and o.get("subtype") == "compact_boundary":
                    w.ev("model", text="Claude 自动压缩了上下文")
                elif k == "result":
                    self.open = max(0, self.open - 1)
                    self.on_usage(o)
                    if self.open:
                        continue                   # 还有排在后面的消息：等它们的回合结束
                    self.turn = None
                    if o.get("is_error") or (o.get("subtype") and o["subtype"] != "success"):
                        err = str(o.get("result") or o.get("errors") or o.get("subtype"))
                        q = core.read_json(QUOTA) or {}
                        if q.get("status") == "rejected" or re.search(r"usage limit|limit reached|out of (extra )?usage", err, re.I):
                            raise RuntimeError(f"Claude 额度用完：{err[:300]}")
                        if self.submitted is None and self.asked is None:
                            fails += 1
                            if fails > int(self.prof.get("retries", 6)):
                                raise RuntimeError(f"Claude 回合连续失败：{err[:300]}")
                            wait = int(min(120, 8 * 2 ** fails) * random.uniform(0.7, 1.3))
                            w.ev("retry", n=fails, wait=wait, err=err[:200])
                            w.sleep_or_stop(wait)
                            self.start_turn(self.next_inputs or ["[crew] 上一回合因接口错误中断，接着做。"])
                            self.next_inputs = []
                            continue
                    fails = 0
                    if w.want:
                        raise w.stop_now()
                    r = self.after_turn()
                    if r:
                        return r
                    nudges += 1
                    if nudges > 3:
                        w.salvage([{"role": "assistant", "content": self.st.get("last_say", "")}], self.turns)
                        return "review", ""
                    self.start_turn(["[系统] 继续工作；完成了就调用 mcp__crewtask__submit_result 提交。"])
        finally:
            self.close()

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, 15)
                except OSError:
                    pass
        if self.sock:
            try:
                self.sock.close()
                os.unlink(self.sock_path)
            except OSError:
                pass
        if self.sock_dir:
            try:
                os.rmdir(self.sock_dir)
            except OSError:
                pass
        if getattr(self, "errlog", None):
            self.errlog.close()

    # ───────── Claude 的消息 → crew 时间线
    def on_usage(self, o):
        u = o.get("usage") or {}
        tin = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
        tout = u.get("output_tokens") or 0
        self.usage["in"] += tin
        self.usage["out"] += tout
        self.usage["cached"] += u.get("cache_read_input_tokens") or 0
        self.w.update(usage=dict(self.usage))
        self.w.ev("turn", n=self.turns, tok_in=tin, tok_out=tout, total=self.usage["in"] + self.usage["out"],
                  think=(u.get("output_tokens_details") or {}).get("thinking_tokens", 0))

    def on_assistant(self, msg):
        w = self.w
        for b in msg.get("content") or []:
            k = b.get("type")
            if k == "text" and (b.get("text") or "").strip():
                w.ev("say", text=b["text"][:8000])
                self.save_state(last_say=b["text"][:2000])
            elif k == "thinking" and (b.get("thinking") or "").strip():
                w.ev("think", text=b["thinking"][:30000])
            elif k == "tool_use":
                name = b.get("name") or ""
                self.step()
                if name.startswith("mcp__crewtask__"):
                    continue                       # dyn_call 自己记
                a = b.get("input") or {}
                arg = a.get("command") if name == "Bash" else json.dumps(a, ensure_ascii=False)
                w.ev("tool", name=name, arg=core.clip(arg or "", 300), full=arg or "")
                w.update(activity=f"第 {self.turns} 步：{name}")
                if name in ("Write", "Edit", "NotebookEdit") and a.get("file_path") and a["file_path"] not in self.files:
                    self.files.append(a["file_path"])

    def on_tool_results(self, msg):
        c = msg.get("content")
        if not isinstance(c, list):
            return
        for b in c:
            if b.get("type") != "tool_result":
                continue
            body = b.get("content")
            out = body if isinstance(body, str) else "\n".join(x.get("text", "") for x in body or [] if isinstance(x, dict) and x.get("type") == "text")
            code = 1 if b.get("is_error") else 0
            self.w.check_alerts(out, code)
            self.w.ev("out", name="tool", code=code, text=out[:4000], chars=len(out), full_out=out)


def run(w):
    return ClaudeRun(w).run()
