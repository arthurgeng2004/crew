"""工人：一个任务一个线程，跑 OpenAI 兼容接口（DeepSeek 等）的工具循环。

工人在这些时候退出线程、把状态交回后台服务：提交成果（→ 待验收）、向组长提问（→ 提问）、
暂停、取消、失败。不设回合上限、不设 token 预算、命令不设超时：工人一直干到自己提交；用量只统计。
每回合后对话落盘，后台服务重启后从 msgs.json 原样续跑。
"""
import base64
import hashlib
import html.parser
import json
import os
import queue
import random
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import core, mcp

WORKERBIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workerbin")

SYSTEM = """你是一个研究工作流里的执行者（工人），组长是 Claude。组长把任务派给你，你做完提交，组长会逐条抽查验收，不合格会退回让你改。

工作纪律：
1. 只做任务里写的事。拿不准任务口径、且不同理解会让结果明显不同时，调用 ask_lead 提问，然后等回答；小事自己按最合理的理解做，并在提交时写明你的理解。
2. 不编造。每个数字、每句引语都要能指到来源（文件路径 + 位置，或网址）。找不到就写"未找到"，并写明查过哪里。
3. 成果写进文件（任务指定的位置；没指定就写在工作目录下），提交时列出文件路径。大文件分段读，或用小脚本提取需要的字段，不要整份打印。
4. 用脚本核对自己的数（加总、口径、单位），把核对结果写进 self_check。
5. 完成后调用 submit_result 恰好一次。summary 要凝练（600 字以内，结论先行，3–8 条要点），组长只读它和你列的文件；过程、明细放进文件。open_issues 每条一句话，写未解决的问题和你做的假设。
6. 收到组长的退回意见时，逐条改，改完重新 submit_result，并在 summary 里逐条说明怎么改的。
7. 工具、数据源、MCP、接口出故障（5xx、连不上、超时、鉴权或额度错误、脚本说服务不可用、结果里带 error 字段）——这不是“没有数据”。立刻调用 report_problem 报告（写明哪个源、原样的错误信息、影响哪部分），组长会马上知道；不要把空结果当成查无此项，不要悄悄换别的来源顶替还当没事。修故障不是你的事：最多重试一次，不要反复重试或等待，不要去查服务器、服务配置，不要翻别的项目找替代材料（除非任务本来就让你用它们）。任务里不依赖这个源的部分照常做；依赖它的写进 open_issues 并标明“因 X 故障未做 / 未核实”，然后提交。
8. 够了就交。验收标准满足了就提交，不要扩大范围：任务没要的数不取，没要求交叉核对就不去翻原件逐个核对（数据服务返回的数本身就是来源，标明服务.工具即可），一个口径上的小疑问写进 open_issues，不要为它再跑十几回合。组长要更多会退回让你补。"""

TOOLS = [
    {"name": "bash", "description": "在工作目录里执行一条 bash 命令，返回退出码和输出（过长的输出会存盘，只给开头结尾和全文路径）。默认 10 分钟超时，到点停掉并报错；确实要跑更久（大批量下载、长脚本）就传 timeout_s（秒）。别跑 find / 这类扫全盘的命令。",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout_s": {"type": "number"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读文本文件（带行号）。offset = 起始行（从 1 开始），limit = 行数（默认 300）。",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "number"}, "limit": {"type": "number"}}, "required": ["path"]}},
    {"name": "write_file", "description": "新建或覆盖一个文件（自动建目录）。",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "把文件里的 old_string（必须恰好出现一次）替换成 new_string。",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["path", "old_string", "new_string"]}},
    {"name": "report_problem", "description": "报告故障：工具 / 数据源 / MCP / 接口坏了（5xx、连不上、超时、鉴权或额度错误、结果带 error）。立刻通知组长，不暂停你的工作。不是用来问口径的（那用 ask_lead）。"
                                       "你之前报的故障恢复了或查明是误报：再调一次，status 写 resolved / false_alarm（带上当时回执里的告警 key 最准），crew 会关掉那条，不会新开。",
     "parameters": {"type": "object", "properties": {"problem": {"type": "string", "description": "哪个源 / 工具 / 地址坏了，附原样错误信息；关告警时写为什么关"},
                                                    "impact": {"type": "string", "description": "对任务的影响：哪些部分做不了、哪些结论可信度下降"},
                                                    "status": {"type": "string", "enum": ["open", "resolved", "false_alarm"], "description": "open = 报新故障（默认）；resolved / false_alarm = 关掉你之前报的那条"},
                                                    "key": {"type": "string", "description": "要关的告警 key（报告时回执里给的，如 报告:1a2b3c4d）"}}, "required": ["problem"]}},
    {"name": "mcp_tools", "description": "查本机 MCP 数据服务：不带参数 = 服务目录；带 server = 这个服务的全部工具和参数；再带 tool = 这个工具的完整说明。调用前不确定参数就先查。",
     "parameters": {"type": "object", "properties": {"server": {"type": "string"}, "tool": {"type": "string"}}}},
    {"name": "mcp_call", "description": "调用一个 MCP 数据工具。args 按 mcp_tools 给的参数写成 JSON 对象。结果太长时全文存盘、只给开头和路径；要留原件或后续用脚本处理就传 save_to（文件路径），全文写进去。出错会原样返回：服务故障（5xx、连不上、超时）不是“没有数据”。",
     "parameters": {"type": "object", "properties": {"server": {"type": "string"}, "tool": {"type": "string"}, "args": {"type": "object"},
                                                    "save_to": {"type": "string", "description": "可选：完整结果写到这个文件（JSON 结果存成缩进 JSON，文本原样）"}},
                    "required": ["server", "tool"]}},
    {"name": "view_image", "description": "看一张图：图片文件（png / jpg / webp / gif），或 PDF 的某一页（page，从 1 开始）。图会附在下一条消息里给你看，看完把读到的数、文字写进文件——图之后会移出上下文。文档里的图表、扫描的表格、PDF 里转不出文字的页都用它。一回合最多看 4 张。",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "page": {"type": "number", "description": "PDF 的页码（从 1 开始）；图片不用写"}}, "required": ["path"]}},
    {"name": "web_fetch", "description": "抓一个网页或文件（官网产品页、规格书、价格页、PDF）。全文存进 save_to（不写就存在工作目录的 _web/ 下），旁边一个 .meta.json 记网址、抓取时间、来源；返回网页正文（文字）。任务有截止日（回测）时只能拿 archive.org 上截止日之前的存档，拿不到就写进 open_issues，不要用现在的网页顶替。",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "save_to": {"type": "string", "description": "可选：存到这个路径（建议存进项目的原件目录）"}}, "required": ["url"]}},
    {"name": "ask_lead", "description": "向组长（Claude）提一个问题，然后暂停等回答。只在口径不清、且不同理解会明显改变结果时用。",
     "parameters": {"type": "object", "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, "required": ["question"]}},
]


def content_len(c):
    """消息内容的长度（字符）：带图的列表里，文字按字数，一张图按 4000 字符算（约 1–2 千 token），不按 base64 长度。"""
    if isinstance(c, list):
        return sum(len(x.get("text") or "") if x.get("type") == "text" else 4000 for x in c if isinstance(x, dict))
    return len(str(c or ""))


def strip_images(msgs, keep=6):
    """上下文里只留最近 keep 张图；更早的换成一行文字（说明看过哪张、要再看用 view_image）。原地改，也返回 msgs。"""
    seen = 0
    for m in reversed(msgs):
        c = m.get("content")
        if not isinstance(c, list):
            continue
        n = sum(1 for x in c if isinstance(x, dict) and x.get("type") == "image_url")
        if not n:
            continue
        if seen + n <= keep:
            seen += n
            continue
        label = " ".join(x.get("text") or "" for x in c if isinstance(x, dict) and x.get("type") == "text")
        m["content"] = f"{label}\n[这几张图已移出上下文；要再看就用 view_image]"
        seen = keep
    return msgs


class _Text(html.parser.HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article", "ul", "ol", "dt", "dd"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.title, self.in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self.in_title = False
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)


def html_text(body):
    """HTML → 正文文字（去掉脚本、样式；段落换行，表格单元格用 | 隔开）。"""
    m = re.search(rb'charset=["\']?([\w-]+)', body[:3000], re.I)
    enc = (m.group(1).decode() if m else "utf-8")
    try:
        s = body.decode(enc, "replace")
    except LookupError:
        s = body.decode("utf-8", "replace")
    p = _Text()
    p.feed(s)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.out))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return (f"标题：{p.title.strip()}\n\n" if p.title.strip() else "") + text


def http_get(url, limit, timeout):
    """GET：走环境里的代理；返回 (内容, Content-Type, 最终网址)。超过 limit 字节报错。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
                                               "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(limit + 1)
        if len(body) > limit:
            raise ValueError(f"超过 {limit // 1_000_000} MB，没存")
        return body, (r.headers.get("Content-Type") or "").lower(), r.geturl()


def opening(t):
    """派活时交给工人的第一条消息：派发方、任务说明、验收标准、先读的材料、工作目录。"""
    o = t.get("origin") or {}
    if o.get("session"):
        via = core.via_label(o.get("via"))
        src = (f"派发方：Claude 对话「{o.get('title') or o.get('first') or '（无标题）'}」" + (f"里的 {via}" if via else "")
               + f"（会话 {o['session']}，目录 {o.get('cwd', '—')}）。"
               "这个任务只属于这个对话：你的提问（ask_lead）和提交都回到这个对话的组长；别的对话、别的任务的文件不要动。")
    else:
        src = f"派发方：{core.origin_label(t)}（不是从 Claude 对话派的）。"
    parts = [f"任务：{t['title']}", src, "", t["prompt"]]
    if t.get("accept"):
        parts += ["", "验收标准（组长会按这些逐条检查）：", t["accept"]]
    if t.get("files"):
        parts += ["", "先读这些材料：", *[f"- {f}" for f in t["files"]]]
    if t.get("wf"):
        parts += ["", f"这是工作流「{t['wf'].get('name')}」里的一步，由脚本自动收下你的成果，没有人能回答提问：拿不准的按最合理的理解做，并写进 open_issues。"]
    if t.get("deliverable"):
        parts += ["", "成果文件（先建骨架，查清一项写一项，不要等全部查完再写）：", *[f"- {f}" for f in t["deliverable"]]]
    if t.get("cutoff"):
        parts += ["", f"截止日：{t['cutoff']}（回测）。只能用这一天及以前公开的信息；web_fetch 只给 archive.org 上截止日之前的存档。"]
    parts += ["", f"工作目录：{t['cwd']}。完成后调用 submit_result。"]
    return "\n".join(parts)


SUMMARY_MAX = 1500  # 超过就退回让工人压缩


def submit_tool(schema):
    props = {
        "summary": {"type": "string", "description": "凝练汇报，600 字以内：第一句是结论；再用 3–8 条要点写关键数字（带出处）、做了什么、没做到什么。过程、明细、长表格写进文件，这里只放组长做判断需要的东西。"},
        "files": {"type": "array", "items": {"type": "string"}, "description": "产出文件路径（最重要的放前面）"},
        "self_check": {"type": "string", "description": "一两句：用脚本或复读核对了什么、结果如何"},
        "open_issues": {"type": "array", "items": {"type": "string"}, "description": "未解决的问题、做过的假设，每条一句话"},
    }
    req = ["summary", "files", "self_check", "open_issues"]
    if schema:
        props["data"] = schema
        req.append("data")
    return {"name": "submit_result", "description": "提交成果（任务完成时调用一次；被退回修改后再调用一次）。",
            "parameters": {"type": "object", "properties": props, "required": req}}


def validate(v, s, at="$"):
    """只覆盖常用子集的 JSON Schema 校验。"""
    if not s:
        return []
    t = s.get("type")
    ok = {"object": lambda x: isinstance(x, dict), "array": lambda x: isinstance(x, list),
          "string": lambda x: isinstance(x, str), "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
          "integer": lambda x: isinstance(x, int) and not isinstance(x, bool), "boolean": lambda x: isinstance(x, bool)}
    if t in ok and not ok[t](v):
        return [f"{at}: 应为 {t}"]
    errs = []
    if "enum" in s and v not in s["enum"]:
        errs.append(f"{at}: 只能是 {s['enum']}")
    if t == "object":
        for k in s.get("required", []):
            if k not in v:
                errs.append(f"{at}.{k}: 缺少")
        for k, ps in (s.get("properties") or {}).items():
            if k in v and v[k] is not None:
                errs += validate(v[k], ps, f"{at}.{k}")
    if t == "array" and s.get("items"):
        for i, x in enumerate(v):
            errs += validate(x, s["items"], f"{at}[{i}]")
    return errs[:20]


# 接口报“上下文超长”的几种说法（各家 OpenAI 兼容接口）
TOO_LONG = re.compile(r"context[_ ]length|maximum context|too long|exceeds? the (model'?s )?(context|maximum)|prompt is too long|input.*too large|exceeds? .*tokens", re.I)


def opener(prof):
    """档案写了 direct（内网网关）就不走环境变量里的代理；否则照常。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({})) if prof.get("direct") else urllib.request.build_opener()


def read_stream(resp):
    """把 OpenAI 兼容接口的流式回复（SSE）拼回和非流式一样的形状：choices[0].message + usage。"""
    msg = {"role": "assistant", "content": "", "reasoning_content": ""}
    calls, usage, finish, done = {}, {}, None, False
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        line = line[5:].strip()
        if line == "[DONE]":
            done = True
            break
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        if chunk.get("error"):
            raise RuntimeError(f"流式回复出错：{json.dumps(chunk['error'], ensure_ascii=False)[:300]}")
        usage = chunk.get("usage") or usage
        for ch in chunk.get("choices") or []:
            finish = ch.get("finish_reason") or finish
            d = ch.get("delta") or {}
            msg["content"] += d.get("content") or ""
            msg["reasoning_content"] += d.get("reasoning_content") or ""
            for tc in d.get("tool_calls") or []:
                c = calls.setdefault(tc.get("index", len(calls)), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                c["id"] = tc.get("id") or c["id"]
                f = tc.get("function") or {}
                c["function"]["name"] += f.get("name") or ""
                c["function"]["arguments"] += f.get("arguments") or ""
    if calls:
        msg["tool_calls"] = [calls[k] for k in sorted(calls)]
    if not done and finish is None:
        # 连接中途断了：半截回复不能当完整回复用，交给外层重试
        raise RuntimeError(f"流式回复中断（已收到 {len(msg['content'])} 字）")
    return {"choices": [{"message": msg, "finish_reason": finish}], "usage": usage}


def load_env_file(path):
    env = {}
    try:
        for line in open(os.path.expanduser(path), encoding="utf-8"):
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


class Stop(Exception):
    def __init__(self, status, note=""):
        super().__init__(note)
        self.status = status
        self.note = note


class Worker(threading.Thread):
    def __init__(self, daemon, task):
        super().__init__(daemon=True, name=f"crew-{task['id']}")
        self.d = daemon
        self.tid = task["id"]
        self.dir = core.task_dir(self.tid)
        self.q = queue.Queue()
        self.cfg = daemon.cfg
        self.task = task
        self.prof = dict(self.cfg["profiles"].get(task.get("profile") or self.cfg["default_profile"]) or {})
        if not self.prof:
            self.prof = {"backend": "mock"}
        self.want = None            # 被打断后要落的状态：paused / cancelled / rejected
        self.want_note = ""
        self.proc = None            # 正在跑的 bash 子进程
        self.clear_pending = False  # 投递来的消息已进对话：下次存盘后再清 pending.json

    # ───────── 状态与事件
    def ev(self, kind, **kw):
        core.append_event(self.tid, kind, **kw)

    def update(self, **kw):
        self.d.update_task(self.tid, **kw)

    def msgs_path(self):
        return os.path.join(self.dir, "msgs.json")

    def save(self, msgs):
        core.write_json(self.msgs_path(), msgs)
        if self.clear_pending:
            # 对话落盘之后才清投递：中间崩了，下次续跑还能读到这些消息
            core.write_json(os.path.join(self.dir, "pending.json"), [])
            self.clear_pending = False

    def stop_now(self):
        return Stop(self.want, self.want_note)

    def interrupt(self, what, note=""):
        """来自后台服务主线程：暂停 / 取消 / 否决。正在跑的 bash 立即杀掉。"""
        self.want_note = note
        self.want = what
        p = self.proc
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass

    # ───────── 工具
    def P(self, p):
        return os.path.abspath(os.path.join(self.task["cwd"], os.path.expanduser(p or "")))

    def persist_out(self, s, n):
        if len(s) <= n:
            return s
        d = os.path.join(self.dir, "toolout")
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:12] + ".txt")
        if not os.path.exists(f):
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(s)
        head = int(n * 0.6)
        return (s[:head] + f"\n…[共 {len(s)} 字符，省略中间 {len(s) - n} 字符]…\n" + s[-(n - head):]
                + f"\n[完整输出存在 {f}：需要细节就用 read_file 带 offset/limit 读，不要重跑命令]")

    def run_bash(self, cmd, timeout):
        # 默认超时 bash_timeout_s（10 分钟；0 = 不限）；工人传 timeout_s 按它的
        timeout = float(timeout) if timeout else (float(self.cfg.get("bash_timeout_s") or 0) or None)
        env = {**os.environ, **(self.cfg.get("worker_env") or {})}
        env["PATH"] = WORKERBIN + os.pathsep + env.get("PATH", "")
        # 登录 shell 的配置可能重排 PATH：命令前再放一次，保证 python3 / pip 是 workerbin 里的
        # 输出写进文件、只等 bash 本身退出：命令里放到后台的进程（cmd & …）还拿着输出也不会卡住这一步；
        # 输出再大也不占后台服务的内存
        d = os.path.join(self.dir, "toolout")
        os.makedirs(d, exist_ok=True)
        of = os.path.join(d, f"bash-{threading.get_ident()}.out")
        with open(of, "wb") as fh:
            self.proc = subprocess.Popen(["bash", "-lc", f'export PATH="{WORKERBIN}:$PATH"\n{cmd}'], cwd=self.task["cwd"], stdout=fh,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        tail = ""
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass
            code, tail = self.proc.wait(), f"\n[超时 {int(timeout or 0)} 秒，已终止。确实要跑更久就传 timeout_s（秒）；卡住的命令（扫全盘、等输入、死循环）换个做法]"
        finally:
            self.proc = None
        text = read_bounded(of, 4_000_000)
        try:
            os.remove(of)
        except OSError:
            pass
        return code, text + tail

    def exec_tool(self, name, a):
        n = self.cfg["tool_output_chars"]
        try:
            if name == "bash":
                code, out = self.run_bash(a["command"], a.get("timeout_s"))
                return self.check_alerts(f"exit {code}\n{self.persist_out(out, n)}", code), code
            if name == "mcp_tools":
                return self.persist_out(mcp.describe(self.cfg, a.get("server"), a.get("tool")), n * 3), 0
            if name == "mcp_call":
                return self.mcp_call(a, n)
            if name == "view_image":
                return self.view_image(a)
            if name == "web_fetch":
                return self.web_fetch(a, n)
            if name == "report_problem":
                if self.closing(a):
                    return self.close_reported(a), 0
                prob = str(a.get("problem") or "").strip()
                text = prob + (f"\n影响：{a['impact']}" if a.get("impact") else "")
                hit = core.scan_alerts(prob, self.cfg)
                key, host = (hit[0][0], hit[0][2]) if hit else ("报告:" + core.alert_digest_key(prob[:80]), "")
                al = core.raise_alert(key, core.clip(prob.split("\n")[0], 80), host, text, "工人报告", self.cfg, self.d.tasks.get(self.tid) or self.task)
                seen = set((self.d.tasks.get(self.tid) or self.task).get("alerts") or []) | {key}
                self.update(alerts=sorted(seen))
                self.ev("alert", text=f"工人报告：{text[:400]}", key=key, important=True)
                return f"已通知组长（告警 {al['title']}，key {al['key']}；之后恢复或查明误报，用 status=resolved / false_alarm 带这个 key 关掉）。继续做能做的；受影响的写进 open_issues。", 0
            if name == "read_file":
                o = max(1, int(a.get("offset") or 1))
                k = min(int(a.get("limit") or 300), 2000)
                got, total = [], 0
                with open(self.P(a["path"]), encoding="utf-8", errors="replace") as f:
                    for total, line in enumerate(f, 1):      # 逐行读，只留要的那一段：大文件不整个读进内存
                        if o <= total < o + k:
                            got.append(line.rstrip("\n")[:2000])
                body = "\n".join(f"{o + i:6d}\t{l}" for i, l in enumerate(got))
                more = f"\n[共 {total} 行；继续读用 offset {o + k}]" if o - 1 + k < total else ""
                return self.persist_out(body, n * 2) + more, 0
            if name == "write_file":
                p = self.P(a["path"])
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(a.get("content") or "")
                return f"已写入 {p}（{len(a.get('content') or '')} 字符）", 0
            if name == "edit_file":
                p = self.P(a["path"])
                s = open(p, encoding="utf-8").read()
                c = s.count(a["old_string"])
                if c != 1:
                    return f"错误：old_string 出现 {c} 次（必须恰好一次）", 1
                with open(p, "w", encoding="utf-8") as f:
                    f.write(s.replace(a["old_string"], a["new_string"], 1))
                return f"已修改 {p}", 0
            return f"错误：没有这个工具 {name}", 1
        except Exception as e:  # noqa: BLE001 — 工具错误原样还给模型
            return f"错误：{type(e).__name__}: {e}", 1

    # ───────── 成果文件提醒：到 nudge_after 回合成果文件还不存在，插话一次
    def deliverable_nudge(self, turns):
        t = self.d.tasks.get(self.tid) or self.task
        paths, n = t.get("deliverable") or [], int(t.get("nudge_after") or 0)
        if not paths or not n or turns < n or t.get("deliv_nudged"):
            return None
        # 任务开始后没新建、也没改写过才算没写（定点重跑时文件是上一轮的旧稿，光看存不存在会漏，11-26 root-09）
        since = float(t.get("started") or t.get("created") or 0)

        def fresh(p):
            try:
                return os.path.getmtime(p) >= since - 2      # 文件时间戳粒度粗，留 2 秒余量
            except OSError:
                return False
        missing = [p for p in paths if not fresh(p)]
        if not missing:
            return None
        self.update(deliv_nudged=turns)
        msg = (f"[crew 提醒] 已经 {turns} 回合了，成果文件这次还没写过（不存在，或仍是开工前的旧稿）：{'、'.join(missing)}。现在先把已经查到的写进去（没查完的标“待补”），"
               "再接着查；之后每查清一项就更新文件。中途断了，没写进文件的都会丢。")
        self.ev("steer", text=msg, frm="crew")
        return msg

    # ───────── 工人关掉自己报过的告警（11-26：工人用 report_problem 写“关闭前述告警 status=false_alarm”，crew 却新开了一条）
    CLOSE_RE = re.compile(r"status\s*[=:：]\s*[\"']?(false_alarm|resolved|closed)|^\s*(关闭|撤销|撤回|解除)(前述|之前|上述|此前)?[^\n]{0,20}告警")

    def closing(self, a):
        st = str(a.get("status") or "").strip().lower()
        if st:
            return st in ("resolved", "false_alarm", "closed")
        return bool(self.CLOSE_RE.search(str(a.get("problem") or "")))

    def close_reported(self, a):
        """只关同一对话里只由工人报过的告警（数据源 / 巡检的告警由巡检或组长关）。指定 key 最准；没指定时文字明显对得上才关；对不上就忽略并列出可关的 key。"""
        t = self.d.tasks.get(self.tid) or self.task
        sess = (t.get("origin") or {}).get("session")
        prob = str(a.get("problem") or "")
        probed = {p.get("key") for p in self.cfg.get("probes") or [] if p.get("key")}
        cands = [x for x in core.list_alerts() if x["status"] in ("open", "quiet") and x.get("source") == "工人报告" and x["key"] not in probed
                 and (x["key"] in (t.get("alerts") or []) or (sess and sess in (x.get("sessions") or [])))]
        key = str(a.get("key") or "").strip()
        if key:
            pick = [x for x in cands if x["key"] == key]
        else:
            grams = lambda s: {s[i:i + 2] for i in range(len(s) - 1)}
            pg = grams(prob)
            score = sorted(((len(grams(x.get("sample") or x.get("label") or "") & pg) / (len(grams(x.get("sample") or x.get("label") or "")) or 1), x) for x in cands),
                           key=lambda z: -z[0])
            pick = [score[0][1]] if score and score[0][0] >= 0.25 and (len(score) == 1 or score[0][0] - score[1][0] >= 0.1) else []
        if not pick:
            listing = "；".join(f"{x['key']}：{x.get('label', '')[:40]}" for x in cands[:6])
            return ("没有对得上的告警，忽略（没有新开告警）。" + (f"你能关的告警：{listing}。要关就带 key 再调一次。" if cands else ""))
        why = ("误报" if str(a.get("status") or "").lower() == "false_alarm" or "false_alarm" in prob else "已恢复") + "（工人关闭）：" + core.clip(prob, 160)
        done = core.resolve_alerts([pick[0]["key"]], why)
        self.ev("alert", text=f"工人关闭告警 {pick[0]['key']}：{core.clip(prob, 300)}", key=pick[0]["key"], important=False)
        return f"已关闭告警 {pick[0]['key']}（{pick[0].get('label', '')[:40]}）。" if done else "那条告警已经不是开着的，不用再关。"

    # ───────── 模型
    def api_key(self):
        p = self.prof
        if p.get("api_key"):
            return p["api_key"]
        env = p.get("api_key_env")
        if not env:
            return ""
        return os.environ.get(env) or load_env_file(p.get("env_file", "")).get(env, "")

    def interruptible(self, fn):
        """在辅助线程里等一个阻塞调用；暂停 / 取消时不等它返回。"""
        box = {}

        def go():
            try:
                box["v"] = fn()
            except BaseException as e:  # noqa: BLE001
                box["e"] = e
        th = threading.Thread(target=go, daemon=True)
        th.start()
        while th.is_alive():
            th.join(timeout=0.5)
            if self.want:
                raise self.stop_now()
        if "e" in box:
            raise box["e"]
        return box["v"]

    def model_params(self):
        """这一回合用哪个模型、哪个思考档位：任务指定 > 模型档案；pro 没开通时用它的 fallback。"""
        t = self.d.tasks.get(self.tid) or self.task
        fb = getattr(self, "fallback", None)
        name = (fb or {}).get("model") or t.get("model") or self.prof.get("model")
        mid, info, alias = core.resolve_model(self.cfg, name)
        if info.get("efforts") == [] or self.prof.get("efforts") == []:
            return mid, None, alias           # 模型不支持思考档位：不发 reasoning_effort
        eff = (self.cfg.get("force_effort") or self.prof.get("force_effort") or (fb or {}).get("effort") or t.get("effort")
               or self.prof.get("effort"))
        return mid, core.fit_effort(eff, info.get("efforts") or self.prof.get("efforts")), alias

    def request_body(self, msgs, tools):
        mid, eff, alias = self.model_params()
        body = {"model": mid, "messages": msgs, "tools": tools, "tool_choice": "auto"}
        if self.prof.get("temperature") is not None:
            body["temperature"] = self.prof["temperature"]
        if self.prof.get("max_tokens") is not None:
            # GPT 推理模型要 max_completion_tokens，其他多数接口用 max_tokens
            body[self.prof.get("max_tokens_field") or "max_tokens"] = self.prof["max_tokens"]
        body.update(self.prof.get("extra_body") or {})
        if eff:
            body["reasoning_effort"] = eff
            if eff != "none":
                body.pop("thinking", None)       # 档案里的“不思考”被任务指定的档位覆盖
        used = f"{alias}·{eff or '默认'}"
        if getattr(self, "_used", None) != used:
            self._used = used
            self.update(model_used=used)
            self.ev("model", text=used)
        return body

    def use_fallback(self, err):
        """模型没开通（404 ModelNotOpen）：按配置换成 fallback（pro → flash·max），记进时间线；换过一次就不再换。"""
        if "ModelNotOpen" not in err or getattr(self, "fallback", None):
            return False
        _, info, alias = self.model_params()
        fb = info.get("fallback")
        if not fb:
            return False
        self.fallback = fb
        self.ev("retry", n=0, wait=0, err=f"{alias} 在这个账号上没开通，改用 {fb.get('model')}·{fb.get('effort')}（开通后新任务自动用回 {alias}）")
        return True

    def chat(self, body):
        p = self.prof
        url = p["base_url"].rstrip("/") + "/chat/completions"
        # 默认流式：整段回复一次性返回时，超过约 75 秒的回复会被远端断开；流式一直有数据就不会
        stream = p.get("stream", True)
        if stream:
            body = {**body, "stream": True, "stream_options": {"include_usage": True}}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        key = self.api_key()
        i = limited = 0
        while i <= int(p.get("retries", 6)):
            if self.want:
                raise self.stop_now()
            err = None
            try:
                req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {}), **p.get("headers", {})})
                op = opener(p)
                if stream:
                    # 流式时超时管的是两段数据之间的间隔，不是整段回复
                    r = self.interruptible(lambda: read_stream(op.open(req, timeout=p.get("stream_idle_timeout_s", 300))))
                else:
                    r = self.interruptible(lambda: json.loads(op.open(req, timeout=p.get("request_timeout_s", 600)).read().decode("utf-8")))
                self.rate_note(True)
                return r
            except Stop:
                raise
            except urllib.error.HTTPError as e:
                txt = e.read().decode("utf-8", "replace")[:400]
                if e.code == 429:
                    # 限流：不限并行时常见，不算失败、不占重试次数；带随机抖动，免得所有工人同一刻一起重试
                    limited += 1
                    self.rate_note(False)
                    self.maybe_switch(limited)
                    wait = int(min(60, 5 * 2 ** min(limited, 4)) * random.uniform(0.5, 1.5))
                    self.ev("retry", n=limited, wait=wait, err=f"限流 429：{txt[:150]}")
                    self.sleep_or_stop(wait)
                    continue
                if e.code < 500:
                    raise RuntimeError(f"HTTP {e.code}: {txt}")
                err = f"HTTP {e.code}: {txt}"
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
            wait = int(min(120, 8 * 2 ** i) * random.uniform(0.75, 1.25))
            i += 1
            self.ev("retry", n=i, wait=wait, err=err[:200])
            self.sleep_or_stop(wait)
        raise RuntimeError(f"接口连续失败：{err}")

    def pname(self):
        return self.task.get("profile") or self.cfg.get("default_profile") or "ds"

    def rate_note(self, ok):
        f = getattr(self.d, "note_rate", None)
        if f:
            f(self.pname(), ok)
        if ok:
            self._limited_since = None
        elif not getattr(self, "_limited_since", None):
            self._limited_since = time.time()

    def maybe_switch(self, limited):
        """这个档案有 fallback_profile（DeepSeek → gpt）：连续被限流超过 fallback_after_s 秒，停下交给后台服务换档案续做。"""
        t = self.d.tasks.get(self.tid) or self.task
        fb = self.prof.get("fallback_profile")
        since = getattr(self, "_limited_since", None)
        if fb and fb in self.cfg["profiles"] and t.get("fallback") is not False and since \
                and time.time() - since >= float(self.prof.get("fallback_after_s") or 180):
            raise Stop("switch", f"{self.pname()} 连续限流 {int(time.time() - since)} 秒，改用 {fb}")

    def sleep_or_stop(self, secs):
        for _ in range(int(secs)):
            if self.want:
                raise self.stop_now()
            time.sleep(1)

    def size(self, msgs):
        return sum(content_len(m.get("content")) + len(json.dumps(m.get("tool_calls") or "", ensure_ascii=False)) for m in msgs)

    def context_limit(self):
        """压缩阈值（token）：接口报过超长时的临时上限 > 任务 > 档案 > 按模型窗口满了再压 > 全局默认。
        满了再压 = 窗口 − 回复上限 − 4 万余量（字符换算 token 有误差，新一回合的输出也要放得下）。"""
        if getattr(self, "_force_limit", None):
            return self._force_limit
        v = self.task.get("context_tokens") or self.prof.get("context_tokens")
        if v:
            return int(v)
        _, info, _ = core.resolve_model(self.cfg, self.task.get("model") or self.prof.get("model"))
        if info.get("window"):
            return int(info["window"]) - int(self.prof.get("max_tokens") or 32768) - 40000
        return int((self.cfg.get("defaults") or {}).get("context_tokens") or 300000)

    def compact(self, msgs):
        """超过上限时一次压到 55%（成段压缩，让前缀尽量稳定、便于接口缓存）。
        上限按 token 算：每回合用接口返回的 prompt_tokens 校准“每字符几个 token”，换算成字符再压。"""
        ratio = getattr(self, "_ratio", None) or 0.55
        limit = int(self.context_limit() / ratio)
        if self.size(msgs) <= limit:
            return msgs
        target, keep = int(limit * 0.55), 10
        before = self.size(msgs)
        # 第一段：旧的图、旧的工具输出和旧的大参数换成存根
        strip_images(msgs, keep=2)
        for m in msgs[2:-keep]:
            if self.size(msgs) <= target:
                break
            if m["role"] == "tool" and len(str(m.get("content") or "")) > 600:
                m["content"] = str(m["content"])[:300] + "\n[早期输出已从上下文移除；全文如需可重新读取文件]"
            for tc in m.get("tool_calls") or []:
                args = tc["function"].get("arguments") or ""
                if len(args) > 600:
                    tc["function"]["arguments"] = json.dumps({"_elided": args[:200] + "…（参数已从上下文移除）"}, ensure_ascii=False)
        # 第二段：仍然太长，就把早期对话总结成一段进度摘要
        if self.size(msgs) > target and len(msgs) > keep + 4:
            cut = len(msgs) - keep
            while cut > 2 and msgs[cut]["role"] == "tool":
                cut -= 1
            old = msgs[2:cut]
            if old:
                digest = json.dumps(strip_images([dict(m) for m in old], keep=0), ensure_ascii=False)[-200000:]
                mid, eff, _ = self.model_params()
                body = {"model": mid, self.prof.get("max_tokens_field") or "max_tokens": 32000,
                        "messages": [{"role": "user", "content": "下面是一个执行者做任务的早期工作记录（JSON）。写一段不超过 1500 字的进度摘要，供它接着干：已完成什么、写了哪些文件（路径）、已确认的关键数字和来源、走不通的路、接下来要做什么。只写摘要。\n\n" + digest}]}
                if eff:
                    body["reasoning_effort"] = eff
                note = ""
                try:
                    r = self.chat(body)
                    note = ((r.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                    self.add_usage(r.get("usage") or {})
                except Stop:
                    raise           # 暂停 / 取消：原样交出去，对话不动
                except Exception as e:  # noqa: BLE001
                    self.ev("error", text=f"上下文摘要失败，这次只压缩旧输出、不截早期对话：{e}"[:500])
                if note.strip():
                    msgs[2:cut] = [{"role": "user", "content": "[上下文压缩：以下是你此前工作的进度摘要，替代早期对话]\n" + note}]
        self.ev("compact", before=int(before * ratio), after=int(self.size(msgs) * ratio), chars=before)
        return msgs

    def add_usage(self, u):
        t = self.d.tasks.get(self.tid) or self.task
        cur = dict(t.get("usage") or {"in": 0, "out": 0, "cached": 0})
        cur["in"] += u.get("prompt_tokens", 0) or 0
        cur["out"] += u.get("completion_tokens", 0) or 0
        cur["cached"] += ((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)) or 0
        self.update(usage=cur)
        return cur

    # ───────── 主循环
    def initial(self):
        t = self.task
        note = self.cfg.get("worker_env_note") or ""
        system = t.get("system") or (SYSTEM + ("\n\n" + note if note else ""))
        first = opening(t)
        if mcp.available(self.cfg):
            first += ("\n\n[crew] 本机的 MCP 数据服务（用 mcp_call 直接调，不用写脚本；参数不确定先 mcp_tools 查）：\n" + mcp.brief(self.cfg)
                      + "\n要点：结果大的传 save_to 存文件再用 python 处理。")
        known = [a for a in core.list_alerts() if a["status"] == "open" and a.get("important")]
        if known:
            # 开工前就告诉工人哪些源现在是坏的：别在上面白耗，用到的部分直接写进 open_issues
            first += "\n\n[crew] 当前已知故障（组长已知道）：\n" + "\n".join(
                f"- {a['title']}，自 {time.strftime('%H:%M', time.localtime(a['first']))} 起：{a.get('sample', '')[:160]}" for a in known[:5]) + \
                "\n依赖这些源的部分不要试、不要找替代：直接写进 open_issues（因此故障未做），其余照常做。"
        return [{"role": "system", "content": system}, {"role": "user", "content": first}]

    def mcp_call(self, a, n):
        server, tool, args = str(a.get("server") or ""), str(a.get("tool") or ""), a.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return "错误：args 要是 JSON 对象", 1
        if not isinstance(args, dict):
            return "错误：args 要是 JSON 对象", 1
        bad = mcp.check_args(self.cfg, server, tool, args)
        if bad:
            return "参数不对，没有调用：" + bad, 1
        tag = hashlib.sha1(json.dumps([server, tool, args], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
        img_dir = (os.path.splitext(self.P(a["save_to"]))[0] + "_img") if a.get("save_to") else os.path.join(self.dir, "toolout", f"mcp-img-{server}-{tool}-{tag}")
        d, err, imgs = self.interruptible(lambda: mcp.call(self.cfg, server, tool, args, float(self.cfg.get("mcp_timeout_s") or 180), img_dir))
        inote = (f"\n[结果里有 {len(imgs)} 张图，已存成文件（{img_dir}/img_001…），原位标成「[图 k：路径]」；"
                 f"图表、扫描的表格要读数就用 view_image 看对应的图]") if imgs else ""
        if err:
            msg = f"错误（{server}.{tool}）：{err}"
            if re.search(r"\b5\d\d\b|UPSTREAM|timed? ?out|refused|unavailable|unreachable|连不上|Bad Gateway|reset by peer", str(err), re.I):
                # 服务坏了，不是参数问题：按故障处理（记告警、通知组长、提示工人）
                msg = f"[CREW-ALERT:mcp-{server}] MCP 服务 {server} 出错：{core.clip(str(err), 200)}\n" + msg
            return self.check_alerts(msg, 1, "MCP 调用"), 1
        if isinstance(d, (dict, list)):
            body, full = json.dumps(d, ensure_ascii=False), json.dumps(d, ensure_ascii=False, indent=1)
        else:
            body = full = str(d)
        shape = (f"顶层键 {list(d)[:15]}" if isinstance(d, dict) else f"列表 {len(d)} 项" if isinstance(d, list) else "文本") + f"，{len(full)} 字符"
        if a.get("save_to"):
            p = self.P(a["save_to"])
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(full)
            return f"已存 {p}（{shape}）。开头：\n{core.clip(body, 1500)}{inote}", 0
        if len(body) <= n:
            return body + inote, 0
        # 太长：缩进 JSON 存盘（read_file 能按行读、python 能直接 json.load），只回开头
        d_ = os.path.join(self.dir, "toolout")
        os.makedirs(d_, exist_ok=True)
        p = os.path.join(d_, f"mcp-{server}-{tool}-{hashlib.sha1(full.encode('utf-8', 'replace')).hexdigest()[:8]}.{'json' if isinstance(d, (dict, list)) else 'txt'}")
        with open(p, "w", encoding="utf-8") as f:
            f.write(full)
        return f"{body[:int(n * 0.7)]}\n…[结果太长（{shape}），全文存在 {p}：用 python 读这个文件取需要的字段，不要重复调用]{inote}", 0

    # ───────── 看图
    def view_image(self, a):
        p = self.P(a.get("path"))
        if not os.path.isfile(p):
            return f"错误：没有这个文件 {p}", 1
        pend = getattr(self, "_images", None)
        if pend is None:
            pend = self._images = []
        if len(pend) >= 4:
            return "错误：这一回合已经看了 4 张，先把读到的写下来，下一回合再看", 1
        page = int(a.get("page") or 1)
        out = os.path.join(self.dir, "toolout", f"view-{hashlib.sha1(f'{p}#{page}'.encode()).hexdigest()[:10]}.jpg")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        r = subprocess.run([os.path.join(WORKERBIN, "python3"), os.path.join(os.path.dirname(os.path.abspath(__file__)), "imgtool.py"),
                            p, out, str(page), str(int(self.cfg.get("image_max_side") or 1600))], capture_output=True, text=True, timeout=120)
        try:
            info = json.loads((r.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return f"错误：转图失败：{(r.stderr or r.stdout)[-300:]}", 1
        if info.get("error"):
            return f"错误：{info['error']}", 1
        with open(out, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        where = f"{p}" + (f" 第 {page}/{info['pages']} 页" if info.get("pages") else "")
        pend.append({"label": where, "url": "data:image/jpeg;base64," + b64})
        return f"图已附在下一条消息里：{where}（{info['w']}×{info['h']}）。看完把读到的数、文字写进文件：图之后会移出上下文。", 0

    def attach_images(self, msgs):
        """这一回合 view_image 看的图：工具回执之后补一条带图的消息（工具消息里放不了图）。只留最近 6 张在上下文里。"""
        pend = getattr(self, "_images", None) or []
        if pend:
            self._images = []
            content = [{"type": "text", "text": "[view_image] " + "；".join(x["label"] for x in pend)}]
            content += [{"type": "image_url", "image_url": {"url": x["url"]}} for x in pend]
            msgs.append({"role": "user", "content": content})
        return strip_images(msgs, keep=int(self.cfg.get("images_in_context") or 6))

    # ───────── 网页
    def web_fetch(self, a, n):
        url = str(a.get("url") or "").strip()
        if not re.match(r"https?://", url):
            return "错误：url 要以 http:// 或 https:// 开头", 1
        cutoff = str((self.d.tasks.get(self.tid) or self.task).get("cutoff") or "")
        meta = {"url": url, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if cutoff:
            # 回测：现在的网页会泄露截止日之后的信息，只能用截止日之前的 archive.org 存档
            day = re.sub(r"\D", "", cutoff)[:8]
            q = urllib.parse.quote(url, safe="")
            cdx = f"https://web.archive.org/cdx/search/cdx?url={q}&to={day}235959&output=json&limit=-1&filter=statuscode:200&fl=timestamp,original"
            try:
                rows = json.loads(self.interruptible(lambda: http_get(cdx, 2_000_000, 60))[0] or b"[]")
            except Exception as e:  # noqa: BLE001
                return f"错误：查 archive.org 存档失败：{type(e).__name__}: {e}", 1
            if len(rows) < 2:
                return (f"archive.org 上没有 {url} 在截止日 {cutoff} 之前的存档。这是回测项目，不能看现在的网页（会泄露截止日之后的信息）："
                        "换别的来源（截止日前的原始文件），或写进 open_issues。"), 1
            snap, orig = rows[-1][0], rows[-1][1]
            fetch = f"https://web.archive.org/web/{snap}id_/{orig}"
            meta.update(source="archive.org", snapshot=snap, cutoff=cutoff, archive_url=fetch)
        else:
            fetch = url
            meta["source"] = "live"
        try:
            body, ctype, final = self.interruptible(lambda: http_get(fetch, int(self.cfg.get("web_max_bytes") or 30_000_000), 90))
        except urllib.error.HTTPError as e:
            return f"错误：HTTP {e.code}（{fetch}）", 1
        except Exception as e:  # noqa: BLE001
            return f"错误：抓取失败：{type(e).__name__}: {e}（{fetch}）", 1
        meta.update(final_url=final, content_type=ctype, bytes=len(body))
        kind = "pdf" if "pdf" in ctype or body[:5] == b"%PDF-" else "html" if "html" in ctype or body[:200].lstrip().lower().startswith((b"<!doctype", b"<html")) else "txt"
        if a.get("save_to"):
            p = self.P(a["save_to"])
        else:
            host = re.sub(r"[^\w.-]", "_", urllib.parse.urlsplit(url).netloc)[:60]
            p = os.path.join(self.task["cwd"], "_web", f"{(meta.get('snapshot') or time.strftime('%Y%m%d'))[:8]}_{host}_{hashlib.sha1(url.encode()).hexdigest()[:8]}.{kind}")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(body)
        with open(p + ".meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        src = f"archive.org 存档 {meta['snapshot']}" if cutoff else f"实时网页，抓取于 {meta['fetched_at']}"
        head = f"已存 {p}（{src}，{len(body)} 字节；说明在 {p}.meta.json）。"
        if kind == "pdf":
            return head + "\n是 PDF：文字用 python（fitz / pypdf）取，图表和扫描页用 view_image 按页看。", 0
        text = html_text(body) if kind == "html" else body.decode("utf-8", "replace")
        return head + "\n正文：\n" + self.persist_out(text, n), 0

    def check_alerts(self, out, code, source="工具输出"):
        """工具输出里有故障特征：记告警（叫醒派活的 Claude 对话），并在这次输出后面提示工人。同一故障在一个任务里只提示一次。"""
        hits = core.scan_alerts(out, self.cfg, code)
        if not hits:
            return out
        seen = set((self.d.tasks.get(self.tid) or self.task).get("alerts") or [])
        notes = []
        for key, label, host, line in hits:
            a = core.raise_alert(key, label, host, line, source, self.cfg, self.d.tasks.get(self.tid) or self.task)
            if key in seen:
                continue
            seen.add(key)
            self.ev("alert", text=f"{a['title']}：{line[:200]}", key=key, important=a["important"])
            notes.append(a["title"])
        if notes:
            self.update(alerts=sorted(seen))
            out += ("\n\n[crew 提示] 这次输出显示数据源 / 服务故障：" + "；".join(notes) + "。这不是“没有数据”：不要把空结果当成查无此项，"
                    "不要悄悄换别的来源顶替。crew 已记下并通知组长（不用再 report_problem 报同一件事）。修故障不是你的事：不要反复重试、不要查服务器配置、"
                    "不要去别处找替代材料。任务里不依赖这个源的部分照常做；依赖它的写进 open_issues（因此故障未做 / 未核实），然后提交。")
        return out

    @staticmethod
    def unanswered(msgs):
        """最后一条带工具调用的 assistant 里，还没有回执的调用 id → 名字。"""
        for i in range(len(msgs) - 1, -1, -1):
            m = msgs[i]
            if m["role"] == "assistant" and m.get("tool_calls"):
                done = {x.get("tool_call_id") for x in msgs[i + 1:] if x["role"] == "tool"}
                return {tc["id"]: tc["function"]["name"] for tc in m["tool_calls"] if tc["id"] not in done}
            if m["role"] == "user":
                return {}
        return {}

    def pending_ask(self, msgs):
        return next((i for i, n in self.unanswered(msgs).items() if n == "ask_lead"), None)

    def pending_inputs(self, msgs):
        """取出投递来的消息：引导、回答、退回意见。"""
        items = []
        pend = core.read_json(os.path.join(self.dir, "pending.json"), [])
        if pend:
            items += pend
            self.clear_pending = True
        items += self.take_queue()
        for it in items:
            k = it.get("kind")
            ask = self.pending_ask(msgs)
            if ask and k in ("answer", "steer"):
                msgs.append({"role": "tool", "tool_call_id": ask, "content": f"{it.get('from') or '组长'}回答：{it['text']}"})
                self.update(question=None)
                self.ev("answer", text=it["text"][:1000])
            elif k == "answer":
                msgs.append({"role": "user", "content": f"[组长补充]\n{it['text']}"})
            elif k == "followup":
                msgs.append({"role": "user", "content": f"[{it.get('from') or '组长'}：下一步（第 {it.get('round')} 步），接着上面的工作做]\n{it['text']}\n\n做完这一步后再调用 submit_result 提交这一步的成果（summary 只写这一步）。"})
            elif k == "revise":
                msgs.append({"role": "user", "content": f"[组长验收：退回修改，第 {it.get('round')} 轮]\n评分：{it.get('score', '—')}\n意见：\n{it['text']}\n\n逐条改完后重新调用 submit_result，在 summary 里逐条说明怎么改的。"})
            else:
                who = it.get("from") or "组长"
                msgs.append({"role": "user", "content": f"[{who}的插话指示]\n{it['text']}"})
        return msgs

    def fix_dangling(self, msgs):
        """续跑时补上没有回执的工具调用（后台服务中途重启）；向组长的提问继续等回答。"""
        for i, name in self.unanswered(msgs).items():
            if name != "ask_lead":
                msgs.append({"role": "tool", "tool_call_id": i, "content": "[后台服务重启，这次调用没有执行完；需要的话重做]"})
        return msgs

    def run(self):
        status, note = "failed", ""
        try:
            status, note = self.loop()
        except Stop as s:
            status, note = s.status, s.note
        except Exception as e:  # noqa: BLE001
            status, note = "failed", f"{type(e).__name__}: {e}"
        finally:
            self.d.worker_done(self.tid, status, note)

    def backend(self):
        """chat（自己的工具循环）还是 codex（Codex app-server）。已经开跑的任务不换：有 Codex 会话就接着 codex，
        有 msgs.json 就接着 chat；新任务按档案（backend: codex）。"""
        if os.path.exists(os.path.join(self.dir, "codex.json")):
            return "codex"
        if os.path.exists(self.msgs_path()):
            return "chat"
        return self.prof.get("backend") or "chat"

    def take_queue(self):
        items = []
        while True:
            try:
                items.append(self.q.get_nowait())
            except queue.Empty:
                return items

    def loop(self):
        t = self.task
        if self.prof.get("backend") == "mock":
            return self.mock()
        if self.backend() == "codex":
            from . import codexrun
            return codexrun.run(self)
        msgs = core.read_json(self.msgs_path()) or self.initial()
        msgs = self.fix_dangling(msgs)
        schema = t.get("schema")
        has_mcp = mcp.available(self.cfg)
        tools = [{"type": "function", "function": x} for x in TOOLS + [submit_tool(schema)]
                 if not (t.get("wf") and x["name"] == "ask_lead") and (has_mcp or not x["name"].startswith("mcp_"))
                 and (x["name"] != "view_image" or self.prof.get("vision"))]
        offered = {x["function"]["name"] for x in tools}
        turns = int(t.get("turns") or 0)
        empty = 0
        while True:
            msgs = self.pending_inputs(msgs)
            if self.pending_ask(msgs):
                self.save(msgs)
                return "waiting", ""
            if self.want:
                self.save(msgs)
                raise self.stop_now()
            if turns and turns % 20 == 0 and t.get("nudged") != turns:
                # 回合多了提醒一次：多数超时的任务是做完了还在扩大范围、反复核对
                t["nudged"] = turns
                self.update(nudged=turns)
                msgs.append({"role": "user", "content": f"[系统] 已用 {turns} 回合。对照验收标准：已经满足就现在提交；还差的只做缺的那部分，不要扩大范围或反复核对。"})
            nudge = self.deliverable_nudge(turns)
            if nudge:
                msgs.append({"role": "user", "content": nudge})
            msgs = self.compact(msgs)
            self.save(msgs)      # 投递来的消息先落盘：这一回合被打断也不会丢
            body = self.request_body(msgs, tools)
            try:
                r = self.chat(body)
            except RuntimeError as e:
                if TOO_LONG.search(str(e)) and not getattr(self, "_force_limit", None):
                    # 估算偏小、真的撑满了窗口：按当前上限的八成当场压一次再发
                    self._force_limit = int(self.context_limit() * 0.8)
                    self.ev("retry", n=0, wait=0, err=f"上下文超长，压缩后重发：{str(e)[:150]}")
                    msgs = self.compact(msgs)
                    self._force_limit = None
                    self.save(msgs)
                    r = self.chat(self.request_body(msgs, tools))
                elif self.use_fallback(str(e)):
                    r = self.chat(self.request_body(msgs, tools))
                else:
                    raise
            turns += 1
            pt = (r.get("usage") or {}).get("prompt_tokens") or 0
            if pt and self.size(msgs) > 20000:
                self._ratio = max(0.2, min(1.5, pt / self.size(msgs)))   # 校准：这一批内容实际多少 token
            u = self.add_usage(r.get("usage") or {})
            self.update(turns=turns, activity=f"第 {turns} 回合")
            m = (r.get("choices") or [{}])[0].get("message") or {}
            am = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                am["tool_calls"] = [{"id": tc["id"], "type": "function", "function": {"name": tc["function"]["name"], "arguments": tc["function"].get("arguments") or "{}"}} for tc in m["tool_calls"]]
            msgs.append(am)
            self.ev("turn", n=turns, tok_in=(r.get("usage") or {}).get("prompt_tokens", 0), tok_out=(r.get("usage") or {}).get("completion_tokens", 0),
                    total=u["in"] + u["out"], think=len(m.get("reasoning_content") or ""))
            if (m.get("reasoning_content") or "").strip():
                # 推理过程只进时间线给人看（面板的“对话”页），不放回消息里发给模型
                self.ev("think", text=m["reasoning_content"][:30000])
            if am["content"].strip():
                self.ev("say", text=am["content"][:8000])
            if not am.get("tool_calls"):
                empty += 1
                if empty > 3:
                    # 连着几次只说话不动手：当它干完了，把最后说的话和写过的文件交给组长验收
                    self.save(msgs)
                    self.salvage(msgs, turns)
                    return "review", ""
                msgs.append({"role": "user", "content": "[系统] 继续工作；完成了就调用 submit_result 提交。"})
                self.save(msgs)
                continue
            empty = 0
            submitted = asked = None
            for tc in am["tool_calls"]:
                name = tc["function"]["name"]
                try:
                    a = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError as e:
                    msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": f"错误：参数不是合法 JSON（{e}）"})
                    continue
                if not isinstance(a, dict):
                    msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": "错误：参数要是 JSON 对象"})
                    continue
                if name not in offered:
                    why = "工作流里的任务不能向组长提问：按最合理的理解做，把拿不准的写进 open_issues" if name == "ask_lead" else f"没有这个工具 {name}"
                    msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": f"错误：{why}"})
                    continue
                if name == "submit_result":
                    errs = validate(a, submit_tool(schema)["parameters"])
                    if not errs and len(str(a.get("summary") or "")) > SUMMARY_MAX:
                        errs = [f"summary 有 {len(a['summary'])} 字，太长：压到 600 字以内（结论 + 3–8 条要点），过程和明细写进文件再列进 files"]
                    if errs:
                        msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": "被拒绝，改好再提交：\n" + "\n".join(errs)})
                        self.ev("tool", name=name, arg="（格式不合格）", code=1)
                    elif submitted is None:
                        submitted = a
                        msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": "已收到，等待组长验收。"})
                    else:
                        msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": "重复提交，已忽略。"})
                    continue
                if name == "ask_lead":
                    asked = a
                    continue      # 回执在回答到达时补上
                full = full_args(name, a)
                self.ev("tool", name=name, arg=preview_args(name, a), **({"full": full} if full else {}))
                out, code = self.exec_tool(name, a)
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
                self.ev("out", name=name, code=code, text=out[:4000], chars=len(out))
                if self.want:
                    break
            # 同一回合里没执行到的调用（被暂停打断、或多个提问只留第一个）补上回执，保持消息序列合法
            asks = [tc["id"] for tc in am["tool_calls"] if tc["function"]["name"] == "ask_lead"]
            for i, name in self.unanswered(msgs).items():
                if name != "ask_lead" or (submitted is not None or i != asks[0]):
                    msgs.append({"role": "tool", "tool_call_id": i, "content": "（未执行）" if name != "ask_lead" else "（提问已忽略：同一回合已提交或已有提问）"})
            msgs = self.attach_images(msgs)
            if asked is not None and submitted is None:
                q = asked.get("question", "")
                opts = asked.get("options") or []
                self.update(question={"text": q, "options": opts, "t": core.now()})
                self.ev("ask", text=q, options=opts)
                self.save(msgs)
                return "waiting", ""
            self.save(msgs)
            if submitted is not None:
                rnd = int((self.d.tasks.get(self.tid) or t).get("round") or 1)
                res = {**submitted, "round": rnd, "t": core.now(), "turns": turns}
                core.write_json(os.path.join(self.dir, "result.json"), res)
                hist = os.path.join(self.dir, f"result.r{rnd}.json")
                core.write_json(hist, res)
                self.ev("submit", round=rnd, summary=submitted.get("summary", "")[:1500], files=submitted.get("files", []))
                return "review", ""

    def salvage(self, msgs, turns):
        """工人没调用 submit_result 就停了：把它写过、改过的文件和最后说的话整理成成果，交给组长验收。"""
        files, last = [], ""
        for m in msgs:
            if m.get("role") == "assistant" and (m.get("content") or "").strip():
                last = m["content"].strip()
            for tc in m.get("tool_calls") or []:
                if tc["function"]["name"] in ("write_file", "edit_file"):
                    try:
                        path = self.P(json.loads(tc["function"].get("arguments") or "{}").get("path"))
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    if path not in files and os.path.isfile(path):
                        files.append(path)
        # 工人常用脚本写文件（不经 write_file）：工作目录里任务开始后改过的文件也算，最多看两层、列 60 个
        since = float(self.task.get("started") or self.task.get("created") or 0)
        root = self.task["cwd"]
        if since and os.path.isdir(root):
            for dirpath, dirnames, names in os.walk(root):
                depth = dirpath[len(root):].count(os.sep)
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("__pycache__", "node_modules")] if depth < 2 else []
                for n in sorted(names):
                    path = os.path.join(dirpath, n)
                    try:
                        if os.path.getmtime(path) >= since and path not in files:
                            files.append(path)
                    except OSError:
                        pass
                if len(files) >= 60:
                    break
            files = files[:60]
        rnd = int((self.d.tasks.get(self.tid) or self.task).get("round") or 1)
        res = {"summary": "（工人停下了但没有调用 submit_result；以下由 crew 自动整理，未经工人自查）\n工人最后说的：" + core.clip(last, 800),
               "files": files, "self_check": "无（没有提交）", "open_issues": ["工人没有提交：文件可能没写完，验收时逐个打开核对"],
               "round": rnd, "t": core.now(), "turns": turns, "auto": True}
        core.write_json(os.path.join(self.dir, "result.json"), res)
        core.write_json(os.path.join(self.dir, f"result.r{rnd}.json"), res)
        self.ev("submit", round=rnd, summary="（自动整理，未提交）写过的文件 " + str(len(files)) + " 个", files=files)

    def mock(self):
        """不调模型：走一遍状态机，供测试面板与插件。"""
        t = self.task
        base = int(t.get("turns") or 0)
        for i in range(3):
            if self.want:
                raise self.stop_now()
            time.sleep(float(os.environ.get("CREW_MOCK_STEP", "0.3")))
            self.ev("tool", name="bash", arg=f"echo step {i}")
            self.ev("out", name="bash", code=0, text=f"step {i}", chars=6)
            self.update(turns=base + i + 1, usage={"in": 1000 * (base + i + 1), "out": 100 * (base + i + 1), "cached": 0})
        pend = core.read_json(os.path.join(self.dir, "pending.json"), [])
        core.write_json(os.path.join(self.dir, "pending.json"), [])
        if "[ask]" in t["prompt"] and not any(p.get("kind") == "answer" for p in pend) and not core.read_json(os.path.join(self.dir, "asked.json")):
            core.write_json(os.path.join(self.dir, "asked.json"), {"t": core.now()})
            self.update(question={"text": "模拟提问：用哪个口径？", "options": ["A", "B"], "t": core.now()})
            self.ev("ask", text="模拟提问：用哪个口径？", options=["A", "B"])
            return "waiting", ""
        rnd = int((self.d.tasks.get(self.tid) or t).get("round") or 1)
        res = {"summary": f"模拟成果（第 {rnd} 轮）；收到的投递：{[p.get('kind') for p in pend]}", "files": [], "self_check": "mock", "open_issues": [], "round": rnd, "t": core.now(), "turns": 3}
        if (self.d.tasks.get(self.tid) or t).get("schema"):
            res["data"] = {"echo": t["prompt"][:30], "round": rnd}
        core.write_json(os.path.join(self.dir, "result.json"), res)
        self.ev("submit", round=rnd, summary=res["summary"], files=[])
        return "review", ""


def read_bounded(path, n):
    """读一个可能很大的输出文件：超过 n 字节只留开头和结尾各 n/4。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size <= n:
                return f.read().decode("utf-8", "replace")
            head = f.read(n // 4)
            f.seek(size - n // 4)
            return (head.decode("utf-8", "replace") + f"\n…[输出共 {size} 字节，中间省略]…\n" + f.read().decode("utf-8", "replace"))
    except OSError as e:
        return f"[读输出失败：{e}]"


def full_args(name, a):
    """“对话”页看的完整参数（写文件只记前 3000 字）；和预览一样短就不重复记。"""
    if name == "bash":
        s = a.get("command", "")
    elif name == "write_file":
        c = str(a.get("content") or "")
        s = f"{a.get('path')}\n{c[:3000]}" + (f"\n…（共 {len(c)} 字符）" if len(c) > 3000 else "")
    elif name == "edit_file":
        s = f"{a.get('path')}\n--- 原文\n{str(a.get('old_string') or '')[:1500]}\n+++ 改成\n{str(a.get('new_string') or '')[:1500]}"
    else:
        s = json.dumps(a, ensure_ascii=False, indent=1)
    s = s[:6000]
    return s if s != preview_args(name, a) else ""


def preview_args(name, a):
    if name == "bash":
        return a.get("command", "")[:300]
    if name == "read_file":
        return f"{a.get('path')}" + (f" @{a.get('offset')}" if a.get("offset") else "")
    if name == "write_file":
        return f"{a.get('path')}（{len(a.get('content') or '')} 字符）"
    if name == "edit_file":
        return f"{a.get('path')}"
    return json.dumps(a, ensure_ascii=False)[:200]
