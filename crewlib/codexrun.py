"""codex 后端：工人跑在 Codex app-server 里（stdio JSON-RPC），用 Codex 原生的工具与能力。

和 chat 后端（worker.py 里自己拼 /chat/completions 循环）相比，这里模型拿到的是 Codex 本身那一套：
shell（exec_command / write_stdin，可交互、后台进程）、apply_patch、view_image、MCP 直连（~/.codex/config.toml
里配的服务，含图片结果）、网页搜索、skills（~/.codex/skills、项目 AGENTS.md，再加档案 codex_skill_roots）、
goals / 计划、上下文自动压缩、Responses 接口的推理项跨回合保留与提示缓存。

crew 自己的工具作为 Codex 的“动态工具”（dynamicTools）挂进去：submit_result、report_problem、ask_lead（工作流里没有），
回测（有截止日）时再加 web_fetch（只给 archive.org 截止日前存档）并关掉 Codex 的实时网页搜索。

状态：任务目录 codex.json 记 thread id；续跑（后台重启、提问回来、退回修改）用 thread/resume 接上同一个 Codex 会话。
组长插话：回合进行中用 turn/steer 当场塞进去，回合之间就作为下一回合的输入。暂停 / 取消：turn/interrupt。
"""
import json
import os
import queue
import random
import re
import signal
import subprocess
import threading
import time
import urllib.parse

from . import core

DEFAULT_BIN = "codex"     # 档案 codex_bin 可改成自己的包装脚本（比如先设代理再 exec codex）

DEV_NOTE = """[crew 工人模式]
你跑在 Codex 里，Codex 的工具都能用（shell、apply_patch、view_image、~/.codex/config.toml 里配的 MCP 数据服务、skills）。
另外有 crew 的工具：submit_result（完成时提交，恰好一次）、report_problem（数据源 / 服务故障时报告）、ask_lead（问组长，问完立刻结束本回合等回答）。
- 不要启动子代理；不要等待用户确认——没有人在终端前，只有组长通过 crew 和你交流。
- MCP 工具结果很大时，用 shell 里的 python 落盘再处理。
- python3 / pip 用 PATH 里的（crew/workerbin：CREW_PYTHON 或系统 python3）。"""


def toml_val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        return "{" + ",".join(f"{k}={toml_val(x)}" for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(toml_val(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


class AppServer:
    """一个 codex app-server 子进程。读线程把回复按 id 分给等待者，其余（通知、服务端请求）进 inbox。"""

    def __init__(self, argv, env, cwd, log_path):
        self.err = open(log_path, "ab")
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err, env=env, cwd=cwd,
                                  start_new_session=True)
        self.inbox = queue.Queue()
        self.waiters = {}
        self.nid = 0
        self.wlock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if "id" in o and "method" not in o:
                q = self.waiters.pop(o["id"], None)
                if q:
                    q.put(o)
                continue
            self.inbox.put(o)
        self.inbox.put({"method": "__eof__"})

    def send(self, o):
        with self.wlock:
            self.p.stdin.write((json.dumps(o, ensure_ascii=False) + "\n").encode("utf-8"))
            self.p.stdin.flush()

    def request(self, method, params, timeout=180, stop=None):
        with self.wlock:
            self.nid += 1
            i = self.nid
        q = self.waiters[i] = queue.Queue()
        self.send({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        end = time.time() + timeout
        while True:
            try:
                o = q.get(timeout=0.5)
                break
            except queue.Empty:
                if self.p.poll() is not None:
                    raise RuntimeError(f"codex app-server 已退出（{method}）")
                if stop:
                    stop()
                if time.time() > end:
                    raise RuntimeError(f"codex app-server 无响应：{method}")
        if "error" in o:
            raise RuntimeError(f"{method} 出错：{json.dumps(o['error'], ensure_ascii=False)[:400]}")
        return o.get("result") or {}

    def notify(self, method, params=None):
        self.send({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def respond(self, i, result=None, error=None):
        self.send({"jsonrpc": "2.0", "id": i, **({"error": error} if error else {"result": result or {}})})

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.p.pid, signal.SIGKILL)
            except OSError:
                pass
        self.err.close()


def item_text(content):
    """MCP 结果 / reasoning 内容 → 纯文本（图片只记一笔）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if "content" in content:
            return item_text(content["content"])
        if content.get("type") == "text":
            return content.get("text") or ""
        if content.get("type") in ("image", "inputImage"):
            return "[图片]"
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        return "\n".join(item_text(x) for x in content)
    return str(content)


class CodexRun:
    def __init__(self, w):
        from . import worker as W
        self.W = W
        self.w = w
        self.t = w.task
        self.prof = w.prof
        self.cfg = w.cfg
        self.state_path = os.path.join(w.dir, "codex.json")
        self.st = core.read_json(self.state_path) or {}
        self.srv = None
        self.turn = None            # 正在进行的回合 id
        self.mine = set()           # 本次开的回合（只算这些回合的用量）
        self.next_inputs = []       # 回合之间收到的插话：下一回合一起发
        self.submitted = None
        self.asked = None
        self.files = []
        self.turns = int(self.t.get("turns") or 0)
        u = self.t.get("usage") or {}
        self.usage = {"in": u.get("in", 0), "out": u.get("out", 0), "cached": u.get("cached", 0)}

    # ───────── 启动
    def env(self):
        env = {**os.environ, **(self.cfg.get("worker_env") or {})}
        env["PATH"] = self.W.WORKERBIN + os.pathsep + env.get("PATH", "")
        if self.prof.get("env_file"):
            env.update(self.W.load_env_file(self.prof["env_file"]))
        host = urllib.parse.urlsplit(self.prof.get("base_url") or "").hostname
        if host and self.prof.get("direct"):
            for k in ("NO_PROXY", "no_proxy"):
                cur = env.get(k, "")
                if host not in cur.split(","):
                    env[k] = (cur + "," if cur else "") + host
        return env

    def overrides(self):
        o = dict(self.prof.get("codex_config") or {})
        if not self.prof.get("codex_subagents"):
            o["features.multi_agent"] = False        # 子代理会绕过 crew 的并发名额（GPT 网关只给 2 个），默认关
        if self.t.get("cutoff"):
            o["web_search"] = "disabled"             # 回测：实时网页会泄露截止日之后的信息，改用 crew 的 web_fetch（archive.org）
        prov = self.prof.get("codex_provider")
        if prov:
            o["model_providers.crew"] = prov
            o["model_provider"] = "crew"
        argv = [os.path.expanduser(self.prof.get("codex_bin") or DEFAULT_BIN), "app-server"]
        for k, v in o.items():
            argv += ["-c", f"{k}={toml_val(v)}"]
        return argv

    def dyn_tools(self):
        W = self.W
        names = ["submit_result", "report_problem"] + ([] if self.t.get("wf") else ["ask_lead"]) + (["web_fetch"] if self.t.get("cutoff") else [])
        spec = {x["name"]: x for x in W.TOOLS + [W.submit_tool(self.t.get("schema"))]}
        out = []
        for n in names:
            x = spec[n]
            desc = x["description"]
            if n == "ask_lead":
                desc += "问完立刻结束本回合（不要再调用别的工具），回答会作为新消息发给你。"
            out.append({"type": "function", "name": n, "description": desc, "inputSchema": x["parameters"]})
        return out

    def developer(self):
        note = self.cfg.get("worker_env_note") or ""
        return (self.t.get("system") or self.W.SYSTEM) + "\n\n" + DEV_NOTE + ("\n\n" + note if note else "")

    def save_state(self, **kw):
        self.st.update(kw)
        core.write_json(self.state_path, self.st)

    def start(self):
        w = self.w
        argv = self.overrides()
        self.srv = AppServer(argv, self.env(), self.t["cwd"], os.path.join(w.dir, "codex-server.log"))
        self.srv.request("initialize", {"clientInfo": {"name": "crew", "title": "crew worker", "version": "1"},
                                        "capabilities": {"experimentalApi": True}}, stop=self.check_stop)
        self.srv.notify("initialized")
        roots = [os.path.expanduser(r) for r in (self.prof.get("codex_skill_roots") or []) if os.path.isdir(os.path.expanduser(r))]
        if roots:
            try:
                self.srv.request("skills/extraRoots/set", {"extraRoots": roots}, stop=self.check_stop)
            except RuntimeError as e:
                w.ev("error", text=f"加 skills 目录失败：{e}")
        mid, eff, alias = w.model_params()
        self.mid, self.eff = mid, eff
        common = {"cwd": self.t["cwd"], "model": mid, "approvalPolicy": "never", "sandbox": "danger-full-access",
                  "developerInstructions": self.developer()}
        if not mid:
            common.pop("model")      # 没指定模型：用 Codex 自己的默认（~/.codex/config.toml 或登录账号的默认模型）
            alias = "Codex 默认模型"
        if self.st.get("thread"):
            try:
                self.srv.request("thread/resume", {"threadId": self.st["thread"], **common}, stop=self.check_stop)
                return False
            except RuntimeError as e:
                w.ev("error", text=f"接不上原来的 Codex 会话（{e}），开新会话")
        r = self.srv.request("thread/start", {**common, "dynamicTools": self.dyn_tools(), "serviceName": "crew"}, stop=self.check_stop)
        self.save_state(thread=r["thread"]["id"], model=mid)
        w.update(model_used=f"{alias}·{eff or '默认'}·codex")
        w.ev("model", text=f"{alias}·{eff or '默认'}（Codex 会话 {r['thread']['id'][:13]}）")
        return True

    def handoff(self):
        """从别的档案（DeepSeek 限流）换过来的任务：交接前一个工人的进展（写过的文件、最近说的话和做的事）。"""
        sw = self.t.get("switched") or {}
        path = os.path.join(self.w.dir, sw.get("msgs") or "")
        msgs = core.read_json(path) if sw.get("msgs") else None
        if not msgs:
            return ""
        files, steps = [], []
        for m in msgs:
            for tc in m.get("tool_calls") or []:
                fn = tc["function"]["name"]
                try:
                    a = json.loads(tc["function"].get("arguments") or "{}")
                except ValueError:
                    a = {}
                if fn in ("write_file", "edit_file") and a.get("path"):
                    p = self.w.P(a["path"])
                    if p not in files:
                        files.append(p)
                steps.append(f"{fn} {core.clip(self.W.preview_args(fn, a), 160)}")
        says = [core.clip(m["content"], 600) for m in msgs if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip()][-4:]
        since = float(self.t.get("started") or 0)
        root = self.t["cwd"]
        if since and os.path.isdir(root):
            for dp, dn, names in os.walk(root):
                dn[:] = [d for d in dn if not d.startswith(".") and d not in ("__pycache__", "node_modules")] if dp[len(root):].count(os.sep) < 2 else []
                for n in names:
                    p = os.path.join(dp, n)
                    try:
                        if os.path.getmtime(p) >= since and p not in files:
                            files.append(p)
                    except OSError:
                        pass
        return ("\n\n[crew 交接] 这个任务原来由 " + str(sw.get("from")) + " 工人在做，因接口限流中断，改由你接着做。"
                f"它做了 {len(steps)} 步。不要从头重复：先看它写过的文件，从没做完的地方接着做。\n"
                + ("写过 / 改过的文件：\n" + "\n".join(f"- {p}" for p in files[:40]) + "\n" if files else "还没有写出文件。\n")
                + ("最近的操作：\n" + "\n".join(f"- {x}" for x in steps[-15:]) + "\n" if steps else "")
                + ("它最近说的：\n" + "\n---\n".join(says) if says else "")
                + f"\n（完整对话记录：{path}）")

    def first_message(self):
        first = self.W.opening(self.t) + self.handoff()
        known = [a for a in core.list_alerts() if a["status"] == "open" and a.get("important")]
        if known:
            first += "\n\n[crew] 当前已知故障（组长已知道）：\n" + "\n".join(
                f"- {a['title']}，自 {time.strftime('%H:%M', time.localtime(a['first']))} 起：{a.get('sample', '')[:160]}" for a in known[:5]) + \
                "\n依赖这些源的部分不要试、不要找替代：直接写进 open_issues（因此故障未做），其余照常做。"
        return first

    # ───────── 投递来的消息（插话、回答、退回意见）→ 文字
    def take_inputs(self, include_pending):
        items = []
        if include_pending:
            pend = core.read_json(os.path.join(self.w.dir, "pending.json"), [])
            if pend:
                items += pend
                self.clear_pending = True
        items += self.w.take_queue()
        texts = []
        for it in items:
            k, who = it.get("kind"), it.get("from") or "组长"
            if k == "answer":
                self.w.update(question=None)
                texts.append(f"[{who}回答你的提问]\n{it['text']}")
            elif k == "followup":
                texts.append(f"[{who}：下一步（第 {it.get('round')} 步），接着上面的工作做]\n{it['text']}\n\n做完这一步后再调用 submit_result 提交这一步的成果（summary 只写这一步）。")
            elif k == "revise":
                texts.append(f"[组长验收：退回修改，第 {it.get('round')} 轮]\n评分：{it.get('score', '—')}\n意见：\n{it['text']}\n\n逐条改完后重新调用 submit_result，在 summary 里逐条说明怎么改的。")
            else:
                texts.append(f"[{who}的插话指示]\n{it['text']}")
        return texts

    def start_turn(self, texts):
        p = {"threadId": self.st["thread"], "input": [{"type": "text", "text": "\n\n".join(texts), "text_elements": []}]}
        if self.mid:
            p["model"] = self.mid
        if self.eff:
            p["effort"] = self.eff
        r = self.srv.request("turn/start", p, stop=self.check_stop)
        self.turn = r["turn"]["id"]
        self.mine.add(self.turn)
        if getattr(self, "clear_pending", False):
            core.write_json(os.path.join(self.w.dir, "pending.json"), [])
            self.clear_pending = False
        self.submitted = self.asked = None

    def check_stop(self):
        if self.w.want:
            raise self.w.stop_now()

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
            nudges = fails = limited = 0
            while True:
                if w.want:
                    self.interrupt()
                    raise w.stop_now()
                more = self.take_inputs(False)
                if more:
                    if self.turn:
                        try:
                            self.srv.request("turn/steer", {"threadId": self.st["thread"], "expectedTurnId": self.turn,
                                                            "input": [{"type": "text", "text": "\n\n".join(more), "text_elements": []}]})
                        except RuntimeError:
                            self.next_inputs += more          # 回合刚好结束：留给下一回合
                    else:
                        self.next_inputs += more
                try:
                    o = self.srv.inbox.get(timeout=0.5)
                except queue.Empty:
                    continue
                m = o.get("method")
                if m == "__eof__":
                    raise RuntimeError("codex app-server 意外退出：" + self.tail_log())
                if "id" in o:
                    self.server_request(o)
                    continue
                pr = o.get("params") or {}
                if m == "item/started":
                    self.item_started(pr.get("item") or {})
                elif m == "item/completed":
                    self.item_completed(pr.get("item") or {})
                elif m == "thread/tokenUsage/updated":
                    if pr.get("turnId") not in self.mine:
                        continue                  # 续接会话时 Codex 会重报上一回合的用量：不重复计
                    last = (pr.get("tokenUsage") or {}).get("last") or {}
                    self.usage["in"] += last.get("inputTokens", 0) or 0
                    self.usage["out"] += last.get("outputTokens", 0) or 0
                    self.usage["cached"] += last.get("cachedInputTokens", 0) or 0
                    w.update(usage=dict(self.usage))
                    w.ev("turn", n=self.turns, tok_in=last.get("inputTokens", 0), tok_out=last.get("outputTokens", 0),
                         total=self.usage["in"] + self.usage["out"], think=last.get("reasoningOutputTokens", 0))
                elif m == "error":
                    err = (pr.get("error") or {}).get("message") or json.dumps(pr, ensure_ascii=False)[:300]
                    if pr.get("willRetry"):
                        w.ev("retry", n=0, wait=0, err=f"Codex 重试：{err[:200]}")
                    else:
                        w.ev("error", text=f"Codex：{err[:400]}")
                elif m == "thread/compacted":
                    w.ev("model", text="Codex 自动压缩了上下文")
                elif m == "turn/completed":
                    turn = pr.get("turn") or {}
                    self.turn = None
                    status = turn.get("status")
                    if status == "failed":
                        err = ((turn.get("error") or {}).get("message") or "未知错误")
                        if re.search(r"429|Too Many Requests|rate.?limit|RateLimit", err, re.I):
                            # 限流：不算失败、不占重试次数（同 chat 后端）
                            limited += 1
                            wait = int(min(120, 10 * 2 ** min(limited, 4)) * random.uniform(0.5, 1.5))
                            w.ev("retry", n=limited, wait=wait, err=f"限流 429：{err[:150]}")
                        else:
                            fails += 1
                            if fails > int(self.prof.get("retries", 6)):
                                raise RuntimeError(f"Codex 回合连续失败：{err[:300]}")
                            wait = min(120, 8 * 2 ** fails)
                            w.ev("retry", n=fails, wait=wait, err=err[:200])
                        w.sleep_or_stop(wait)
                        self.start_turn(self.next_inputs or ["[crew] 上一回合因接口错误中断，接着做。"])
                        self.next_inputs = []
                        continue
                    fails = limited = 0
                    if status == "interrupted" and w.want:
                        raise w.stop_now()
                    r = self.after_turn()
                    if r:
                        return r
                    if self.next_inputs:
                        texts, self.next_inputs = self.next_inputs, []
                        nudges = 0
                        self.start_turn(texts)
                        continue
                    nudges += 1
                    if nudges > 3:
                        w.salvage([{"role": "assistant", "content": self.st.get("last_say", "")}], self.turns)
                        return "review", ""
                    self.start_turn(["[系统] 继续工作；完成了就调用 submit_result 提交。"])
        finally:
            if self.srv:
                self.srv.close()

    def interrupt(self):
        if self.turn and self.srv:
            try:
                self.srv.request("turn/interrupt", {"threadId": self.st["thread"], "turnId": self.turn}, timeout=20)
            except RuntimeError:
                pass

    def tail_log(self):
        try:
            with open(os.path.join(self.w.dir, "codex-server.log"), "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 600))
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def after_turn(self):
        """回合结束：交了就待验收，问了就等回答；都没有返回 None。"""
        w, t = self.w, self.t
        if self.submitted is not None:
            rnd = int((w.d.tasks.get(w.tid) or t).get("round") or 1)
            res = {**self.submitted, "round": rnd, "t": core.now(), "turns": self.turns}
            core.write_json(os.path.join(w.dir, "result.json"), res)
            core.write_json(os.path.join(w.dir, f"result.r{rnd}.json"), res)
            w.ev("submit", round=rnd, summary=self.submitted.get("summary", "")[:1500], files=self.submitted.get("files", []))
            return "review", ""
        if self.asked is not None:
            q, opts = self.asked.get("question", ""), self.asked.get("options") or []
            w.update(question={"text": q, "options": opts, "t": core.now()})
            w.ev("ask", text=q, options=opts)
            return "waiting", ""
        return None

    # ───────── Codex 发来的请求
    def server_request(self, o):
        m, i, pr = o.get("method"), o["id"], o.get("params") or {}
        if m == "item/tool/call":
            name, a = pr.get("tool"), pr.get("arguments") or {}
            text, ok = self.dyn_call(name, a if isinstance(a, dict) else {})
            self.srv.respond(i, {"contentItems": [{"type": "inputText", "text": text}], "success": ok})
        elif m in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            self.srv.respond(i, {"decision": "accept"})
        elif m in ("execCommandApproval", "applyPatchApproval"):
            self.srv.respond(i, {"decision": "approved"})
        elif m == "item/tool/requestUserInput":
            ans = {q.get("id"): {"answers": ["（没有人在终端前：按最合理的理解继续；真要问组长用 ask_lead）"]} for q in pr.get("questions") or []}
            self.srv.respond(i, {"answers": ans})
        elif m == "mcpServer/elicitation/request":
            self.srv.respond(i, {"action": "decline"})
        else:
            self.srv.respond(i, error={"code": -32601, "message": f"crew 不处理 {m}"})

    def dyn_call(self, name, a):
        w, W = self.w, self.W
        self.w.ev("tool", name=name, arg=W.preview_args(name, a))
        if name == "submit_result":
            errs = W.validate(a, W.submit_tool(self.t.get("schema"))["parameters"])
            if not errs and len(str(a.get("summary") or "")) > W.SUMMARY_MAX:
                errs = [f"summary 有 {len(a['summary'])} 字，太长：压到 600 字以内（结论 + 3–8 条要点），过程和明细写进文件再列进 files"]
            if errs:
                w.ev("out", name=name, code=1, text="格式不合格", chars=0)
                return "被拒绝，改好再提交：\n" + "\n".join(errs), False
            if self.submitted is not None:
                return "重复提交，已忽略。", True
            self.submitted = a
            return "已收到，等待组长验收。现在结束本回合，不要再调用工具。", True
        if name == "ask_lead":
            if self.submitted is not None or self.asked is not None:
                return "（提问已忽略：本回合已提交或已有提问）", False
            self.asked = a
            return "问题已转给组长。现在结束本回合（不要再调用工具），回答会作为新消息发给你。", True
        if name in ("report_problem", "web_fetch"):
            out, code = w.exec_tool(name, a)
            w.ev("out", name=name, code=code, text=out[:4000], chars=len(out))
            return out, code == 0
        return f"错误：没有这个工具 {name}", False

    # ───────── Codex 的条目 → crew 时间线
    def item_started(self, it):
        k = it.get("type")
        if k == "commandExecution":
            self.w.ev("tool", name="bash", arg=core.clip(it.get("command") or "", 300), full=it.get("command") or "")
            self.w.update(activity=f"第 {self.turns} 步：命令")
        elif k == "mcpToolCall":
            args = json.dumps(it.get("arguments") or {}, ensure_ascii=False)
            self.w.ev("tool", name="mcp_call", arg=f"{it.get('server')}.{it.get('tool')} {core.clip(args, 200)}", full=args)
            self.w.update(activity=f"第 {self.turns} 步：{it.get('server')}.{it.get('tool')}")

    def step(self):
        self.turns += 1
        self.w.update(turns=self.turns, activity=f"第 {self.turns} 步")
        nudge = self.w.deliverable_nudge(self.turns)
        if nudge:
            self.w.q.put({"kind": "steer", "text": nudge, "from": "crew"})      # 主循环取出后 turn/steer 插进当前回合

    def item_completed(self, it):
        w, k = self.w, it.get("type")
        n = self.cfg["tool_output_chars"]
        if k == "agentMessage":
            txt = it.get("text") or ""
            if txt.strip():
                w.ev("say", text=txt[:8000])
                self.save_state(last_say=txt[:2000])
        elif k == "reasoning":
            txt = item_text(it.get("summary")) + ("\n" + item_text(it.get("content")) if it.get("content") else "")
            if txt.strip():
                w.ev("think", text=txt[:30000])
        elif k == "commandExecution":
            self.step()
            out = it.get("aggregatedOutput") or ""
            code = it.get("exitCode")
            code = 1 if code is None and it.get("status") == "failed" else (code or 0)
            w.check_alerts(out, code)
            w.ev("out", name="bash", code=code, text=out[:4000], chars=len(out))
        elif k == "mcpToolCall":
            self.step()
            err = it.get("error")
            out = item_text(it.get("result")) if not err else f"错误：{item_text(err)}"
            w.check_alerts(out, 1 if err else 0)
            w.ev("out", name="mcp_call", code=1 if err else 0, text=out[:4000], chars=len(out))
        elif k == "fileChange":
            self.step()
            paths = [c.get("path") for c in it.get("changes") or [] if c.get("path")]
            self.files += [p for p in paths if p not in self.files]
            w.ev("tool", name="apply_patch", arg=", ".join(paths)[:300])
            w.ev("out", name="apply_patch", code=0 if it.get("status") == "completed" else 1, text=it.get("status") or "", chars=0)
        elif k == "webSearch":
            self.step()
            w.ev("tool", name="web_search", arg=core.clip(it.get("query") or json.dumps(it.get("action") or {}, ensure_ascii=False), 300))
        elif k == "imageView":
            self.step()
            w.ev("tool", name="view_image", arg=it.get("path") or "")
        elif k == "dynamicToolCall":
            self.step()
        elif k == "contextCompaction":
            w.ev("model", text="Codex 自动压缩了上下文")


def run(w):
    return CodexRun(w).run()
