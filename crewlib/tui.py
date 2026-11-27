"""crew 面板（curses，SSH 里可用）。四页：1 主页、2 工作流、3 任务、4 帮助；页内 ↑↓←→ 移动，Enter 整页打开选中项，Esc 返回。

主页：↑↓ 选、Enter 进入（工作流 → 进度树；对话 → 只看那个对话的任务；等人处理的任务 → 直接打开）
工作流页：↑↓ 选工作流，Tab 切到代理列表（↑↓ 选代理，Enter 打开它的对话 / 时间线），v 显示每个代理最近几步，
          C 取消工作流，R 续跑，PgUp PgDn 翻
任务页：
  ↑↓ / j k   选任务          Tab        切换 时间线 / 成果
  PgUp PgDn  翻时间线        End        回到最新（跟随）
  m 插话      a 回答提问      v 验收（通过 / 退回修改 / 否决）
  p 暂停/继续 c 取消          x 失败任务重试    D 删除失败任务
  n 新任务    h 只看需要处理的 / 全部       q 退出
"""
import curses
import json
import locale
import os
import time

from . import cli, core, wf

KEYS = """面板按键
  1 主页   2 工作流   3 任务   4 帮助   Esc 返回上一层   q 退出   U 启动后台
  在 1 / 2 / 3 页里：↑↓←→ 在本页移动；选中一项按 Enter，整页打开它（只有这一项的全部内容），Esc 回来
主页
  ↑↓ 选择   ←→ 跳到上一块 / 下一块（工作流 · 对话 · 等处理的任务）
  Enter：工作流 → 工作流详情；任务 → 任务详情；对话 → 只看那个对话派的任务
工作流（下方预览进度树）
  ↑↓ 选工作流 / 选代理   ←→（或 Tab）在工作流列表和代理之间切换   v 预览里每个代理最近几步
  Enter：选中工作流 → 工作流详情；选中代理 → 它的任务详情      C 取消   R 续跑   PgUp/PgDn 翻预览
任务（下方预览时间线 / 成果）
  ↑↓ 选任务   ← 时间线  → 成果（Tab 也能切）   PgUp/PgDn 翻预览   End 回到最新
  Enter → 任务详情   m 插话   a 回答提问   v 验收   p 暂停 / 继续   c 取消   x 失败的续跑   D 删除失败的   n 新任务   h 只看要处理的 / 全部
任务详情（整页）
  任务信息（状态、轮次、token、时间、派发方、模型、目录、材料、验收标准）→ 全部往来：Claude 的派活 / 插话 / 回答 / 验收，
  工人每一回合的推理（∴）、说的话（●）、工具调用和完整参数（▶）、输出（◀）、提交（◆），crew 的状态变化 → 最新成果全文（自查、未解决、文件、data）
  ↑↓ 滚动   ←→ / PgUp PgDn 翻页   Home 开头   End 最新（运行中自动跟着走）   r 推理 显示 / 隐藏   o 工具输出 展开 / 收起
  m 插话   a 回答   v 验收   Esc 返回
工作流详情（整页）
  信息（状态、说明、派发方、目录、脚本、时间、代理统计、参数）→ 每个阶段的每个代理（状态、用时、回合、token、当前一步、结论、最近几步）
  → 全部日志 → 错误 → 返回值全文
  ↑↓ 选代理   Enter 打开代理的任务详情   ←→ / PgUp PgDn 翻页   s 每个代理显示 0 / 5 / 15 步   o 返回值展开   C 取消   R 续跑   Esc 返回
"""

COLORS = {"running": 2, "queued": 0, "paused": 8, "waiting": 5, "review": 3, "accepted": 6, "rejected": 8, "failed": 1, "cancelled": 8}
GLYPH = {"running": "▶", "queued": "…", "paused": "‖", "waiting": "?", "review": "◆", "accepted": "✓", "rejected": "×", "failed": "✗", "cancelled": "·"}


class App:
    def __init__(self, scr):
        self.scr = scr
        self.sel = 0
        self.view = "log"          # log | result
        self.scroll = 0            # 时间线离底部的行数；0 = 跟随
        self.only_attention = False
        self.flash = ""
        self.flash_t = 0
        self.tasks = []
        self.last_load = 0
        self.page = "home"         # home | wf | tasks
        self.conv = ""             # 任务页只看某个对话派的
        self.jump = ""             # 从工作流点进来的代理任务（平时任务页不列工作流里的任务）
        self.hsel = 0              # 主页选中第几项
        self.home_items = []
        self.wfs = []
        self.wsel = 0
        self.wfocus = "list"       # list | agents
        self.asel = 0
        self.wverbose = 0
        self.wscroll = 0
        self.tree = None

    # ───────── 数据
    def load(self, force=False):
        if not force and time.time() - self.last_load < 1:
            return
        self.last_load = time.time()
        ts = core.list_tasks()
        cutoff = core.now() - 86400
        ts = [t for t in ts if t["status"] not in core.FINAL or t.get("updated", 0) > cutoff]
        self.all_tasks = [t for t in ts if not t.get("wf")]
        ts = [t for t in ts if not t.get("wf") or t["id"] == self.jump]
        if self.conv:
            ts = [t for t in ts if conv_key(t) == self.conv or t["id"] == self.jump]
        ws = [w for w in (wf.load(i) for i in wf.list_ids()) if w]
        ws = [w for w in ws if w["status"] not in wf.DONE or core.now() - (w.get("finished") or 0) < 86400]
        ws.sort(key=lambda w: (w["status"] in wf.DONE, -(w.get("started") or w.get("created") or 0)))
        cur_w = self.wfs[self.wsel]["id"] if self.wfs and self.wsel < len(self.wfs) else None
        self.wfs = ws
        if cur_w in [w["id"] for w in ws]:
            self.wsel = [w["id"] for w in ws].index(cur_w)
        self.wsel = max(0, min(self.wsel, len(ws) - 1))
        self.tree = wf.tree(ws[self.wsel], steps=self.wverbose) if ws and self.page == "wf" else None
        if self.only_attention:
            ts = [t for t in ts if t["status"] in core.NEEDS_LEAD]
        # 需要处理的排前面，其次运行中，最后已结束
        order = {"waiting": 0, "review": 1, "failed": 2, "running": 3, "paused": 4, "queued": 5}
        ts.sort(key=lambda t: (order.get(t["status"], 9), -(t.get("needs_since") or t.get("created", 0)) if t["status"] in core.NEEDS_LEAD else t.get("created", 0)))
        cur = self.current()
        self.tasks = ts
        if cur:
            ids = [t["id"] for t in ts]
            if cur["id"] in ids:
                self.sel = ids.index(cur["id"])
        self.sel = max(0, min(self.sel, len(ts) - 1))

    def current(self):
        return self.tasks[self.sel] if self.tasks and 0 <= self.sel < len(self.tasks) else None

    def say(self, msg):
        self.flash, self.flash_t = msg, time.time()

    # ───────── 绘制
    def put(self, y, x, s, attr=0, width=None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        width = min(width if width is not None else w - x, w - x)
        s = core.clip(s, width) if core.swidth(s) > width else s
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:
            pass

    def color(self, status):
        n = COLORS.get(status, 0)
        if n == 8:
            return curses.A_DIM
        return curses.color_pair(n) if n else 0

    def tabs(self, y, w):
        """页签行：1 主页 2 工作流 3 任务。"""
        run_w = sum(1 for x in self.wfs if x["status"] not in wf.DONE)
        live = sum(1 for t in self.all_tasks if t["status"] not in core.FINAL)
        x = 1
        for key, name, label in (("1", "home", "主页"), ("2", "wf", f"工作流 {run_w}"), ("3", "tasks", f"任务 {live}"), ("4", "help", "帮助")):
            s = f" {key} {label} "
            self.put(y, x, s, curses.A_REVERSE | curses.A_BOLD if ({"conv": "tasks", "wfd": "wf"}.get(self.page, self.page)) == name else curses.A_DIM)
            x += core.swidth(s) + 2
        if self.conv and self.page == "tasks":
            self.put(y, x + 2, f"只看「{conv_name(next((t for t in self.all_tasks if conv_key(t) == self.conv), {'origin': {}, 'owner': self.conv}))}」派的（Esc 清除）", curses.color_pair(4))

    def header(self, w):
        info = core.daemon_info()
        up = f"● 后台运行中（pid {info['pid']}，关掉 Claude 也不停）" if info and info["alive"] else "○ 后台没在运行：按 U 启动"
        self.put(0, 0, core.pad(f" crew  {up}", w), curses.A_REVERSE)

    def footer(self, h, w, text):
        if self.flash and time.time() - self.flash_t < 6:
            text = " " + self.flash + " "
        self.put(h - 1, 0, core.pad(text, w - 1), curses.A_REVERSE)

    def draw(self):
        if self.page == "help":
            return self.draw_help()
        if self.page == "home":
            return self.draw_home()
        if self.page == "wf":
            return self.draw_wf()
        if self.page == "conv":
            return self.draw_conv()
        if self.page == "wfd":
            return self.draw_wfd()
        return self.draw_tasks()

    def draw_help(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        self.header(w)
        self.tabs(1, w)
        lines = (KEYS + "\n" + cli.HELP).split("\n")
        self.hscroll = max(0, min(getattr(self, "hscroll", 0), len(lines) - (h - 4)))
        for i, line in enumerate(lines[self.hscroll:self.hscroll + h - 4]):
            bold = line and not line.startswith(" ")
            self.put(3 + i, 1, line, curses.A_BOLD | curses.color_pair(4) if bold else 0)
        self.footer(h, w, " ↑↓ 翻  Esc 返回  1 主页  2 工作流  3 任务  q 退出 ")
        self.scr.refresh()

    def draw_home(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        self.header(w)
        self.tabs(1, w)
        items = []
        y = 3
        for al in [x for x in core.list_alerts() if x["status"] == "open"][:3]:
            # 数据源 / 服务故障放在最上面
            self.put(y, 1, f"⚠ 故障：{cli.alert_line(al)}", curses.A_BOLD | self.color("failed"))
            y += 1
        ws = self.wfs
        cnt = lambda sts: sum(1 for x in ws if x["status"] in sts)
        self.put(y, 1, "工作流", curses.A_BOLD | curses.color_pair(3))
        self.put(y, 9, f"运行 {cnt(('running', 'starting'))} · 完成 {cnt(('done',))} · 失败 {cnt(('failed',))} · 取消 {cnt(('cancelled',))}（24 小时内）", curses.A_DIM)
        y += 1
        if not ws:
            self.put(y, 3, "还没有工作流：Claude 写好脚本后用 mcp__crew__workflow 交给 crew，或终端 crew wf run 脚本.js", curses.A_DIM)
            y += 1
        for x in ws[:5]:
            r = wf.row(x)
            c = r["counts"]
            items.append(("wf", x["id"], y))
            g = {"running": "▶", "done": "✓", "failed": "✗", "cancelled": "·"}.get(x["status"], "…")
            self.put(y, 3, f"{g} {core.pad(x['name'], 28)} {core.pad(wf.ZH.get(x['status'], x['status']), 5)}{core.pad(core.fmt_secs(r['elapsed']), 8)}"
                     f"代理 {c.get('done', 0)}/{r['agents']}{' ✗' + str(c['failed']) if c.get('failed') else ''}  {core.fmt_tokens(r['tokens'])}  〔{r['origin']}〕",
                     self.color({"running": "running", "failed": "failed", "done": "accepted"}.get(x["status"], "queued")))
            y += 1
        if len(ws) > 5:
            self.put(y, 3, f"…另 {len(ws) - 5} 个：按 2 看全部", curses.A_DIM)
            y += 1
        y += 1
        ts = self.all_tasks
        tc = lambda st: sum(1 for t in ts if t["status"] == st)
        self.put(y, 1, "任务", curses.A_BOLD | curses.color_pair(4))
        self.put(y, 15, f"运行 {tc('running')} · 排队 {tc('queued')} · 待验收 {tc('review')} · 提问 {tc('waiting')} · 失败 {tc('failed')}", curses.A_DIM)
        y += 1
        groups = {}
        for t in ts:
            if t["status"] in core.FINAL:
                continue
            groups.setdefault(conv_key(t), []).append(t)
        if groups:
            self.put(y, 3, "按对话（Enter 只看那个对话的）：", curses.A_DIM)
            y += 1
        for k, g in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:5]:
            items.append(("conv", k, y))
            bits = " · ".join(f"{core.STATUS_ZH[s]} {sum(1 for t in g if t['status'] == s)}" for s in ("running", "queued", "review", "waiting", "failed", "paused") if any(t["status"] == s for t in g))
            self.put(y, 5, f"{core.pad(conv_name(g[0]), 44)} {bits}")
            y += 1
        if not groups:
            self.put(y, 3, "没有进行中的代理", curses.A_DIM)
            y += 1
        needs = [t for t in ts if t["status"] in core.NEEDS_LEAD][:5]
        if needs:
            self.put(y, 3, "等人处理：", curses.A_DIM)
            y += 1
        for t in needs:
            items.append(("task", t["id"], y))
            self.put(y, 5, f"{GLYPH.get(t['status'])} {core.pad(core.STATUS_ZH[t['status']], 6)} {core.pad(t['title'], 40)} {core.fmt_secs(core.elapsed(t))}  〔{conv_name(t)}〕",
                     self.color(t["status"]))
            y += 1
        self.home_items = items
        self.hsel = max(0, min(self.hsel, len(items) - 1))
        if items:
            iy = items[self.hsel][2]
            self.put(iy, 1, "▸", curses.A_BOLD | curses.color_pair(3))
        self.footer(h, w, " ↑↓ 选择  ←→ 换一块  Enter 打开  1 主页 2 工作流 3 任务 4 帮助  q 退出 ")
        self.scr.refresh()

    def draw_wf(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        self.header(w)
        self.tabs(1, w)
        y = 3
        if not self.wfs:
            self.put(y, 2, "没有工作流（24 小时内）。Claude 用 mcp__crew__workflow 交给 crew，或终端 crew wf run 脚本.js", curses.A_DIM)
            self.footer(h, w, " 1 主页  3 任务  4 帮助  q 退出 ")
            self.scr.refresh()
            return
        for i, x in enumerate(self.wfs[:5] if self.wsel < 5 else self.wfs[self.wsel - 4:self.wsel + 1]):
            idx = self.wfs.index(x)
            r = wf.row(x)
            attr = curses.A_REVERSE if idx == self.wsel and self.wfocus == "list" else (curses.A_BOLD if idx == self.wsel else 0)
            g = {"running": "▶", "done": "✓", "failed": "✗", "cancelled": "·"}.get(x["status"], "…")
            self.put(y, 1, core.pad(f"{g} {core.pad(x['name'], 26)} {core.pad(wf.ZH.get(x['status'], x['status']), 5)}{core.pad(core.fmt_secs(r['elapsed']), 8)}"
                                    f"{r['counts'].get('done', 0)}/{r['agents']}  {core.fmt_tokens(r['tokens'])}  {x['id']}  〔{r['origin']}〕", w - 2), attr)
            y += 1
        if len(self.wfs) > 5:
            self.put(y, 1, f"（共 {len(self.wfs)} 个）", curses.A_DIM)
            y += 1
        self.put(y, 0, "─" * w, curses.A_DIM)
        y += 1
        tr = self.tree
        lines = []          # (文字, 属性, 代理序号或 None)
        if tr:
            x = tr["wf"]
            lines.append((f"{x['name']} · {wf.ZH.get(x['status'])} · {core.fmt_secs(x['elapsed'])} · 代理 {x['agents']} · {core.fmt_tokens(x['tokens'])} tok"
                          + (f" · 第 {tr['attempt']} 次启动" if tr.get("attempt", 1) > 1 else "") + f" · 目录 {tr.get('cwd')}", curses.A_BOLD, None))
            if tr.get("description"):
                lines.append(("  " + tr["description"], curses.A_DIM, None))
            k = 0
            self._agents = []
            for p in tr["phases"]:
                g = {"done": "✓", "running": "▶", "pending": "·"}[p["status"]]
                lines.append((f"{g} {p['title']}  {p['done']}/{p['total']}" + (f"  失败 {p['failed']}" if p["failed"] else "")
                              + (f"  {core.fmt_secs(p['elapsed'])}" if p["total"] else "") + (f"  — {p['detail']}" if p["detail"] else ""),
                              curses.A_BOLD | (curses.color_pair(2) if p["status"] == "running" else 0), None))
                for a in p["agents"]:
                    st = a["status"]
                    tag = (f"[{a['model']}] " if a.get("model") else "") + ("缓存 " if a["cached"] else "") + ("续上文 " if a.get("cont") else "") + (f"重试{a['retries']} " if a["retries"] else "")
                    if st in ("running", "queued", "retry", "paused"):
                        tail = (a.get("step") or "") + (f"（{core.fmt_secs(a['step_ago'])}前）" if a.get("step_ago") is not None else "")
                    elif st == "failed":
                        tail = "✗ " + (a.get("note") or "")
                    elif st == "cancelled":
                        tail = "已取消"
                    else:
                        tail = "→ " + (a.get("summary") or "")
                    color = {"running": "running", "retry": "review", "failed": "failed", "done": "accepted", "cancelled": "cancelled"}.get(st, "queued")
                    lines.append((f"   {wf.GLYPH.get(st, ' ')} {core.pad(str(a['n']), 4)}{core.pad(a['label'], 28)} {core.pad(wf.AZH.get(st, st), 5)}"
                                  f"{core.pad(core.fmt_secs(a['elapsed']), 7)}{core.pad(str(a['turns'] or 0) + '回合', 7)}{core.pad(core.fmt_tokens(a['tokens']), 6)}{tag}{tail}",
                                  self.color(color), k))
                    self._agents.append(a)
                    for s in a.get("recent") or []:
                        lines.append(("        " + s, curses.A_DIM, None))
                    k += 1
            if tr.get("logs"):
                lines.append(("日志：", curses.A_DIM, None))
                lines += [(f"  {core.stamp(l['t'])} {l['msg']}", curses.A_DIM, None) for l in tr["logs"][-6:]]
            if tr.get("error"):
                lines.append(("错误：" + tr["error"].splitlines()[0], curses.color_pair(1), None))
            if tr.get("result_preview") and x["status"] == "done":
                lines.append(("返回值：" + tr["result_preview"], curses.A_DIM, None))
        self.asel = max(0, min(self.asel, len(getattr(self, "_agents", [])) - 1))
        body_h = h - y - 1
        if self.wfocus == "agents":
            at = next((i for i, l in enumerate(lines) if l[2] == self.asel), 0)
            if at - self.wscroll >= body_h - 1:
                self.wscroll = at - body_h + 2
            if at < self.wscroll:
                self.wscroll = max(0, at - 1)
        self.wscroll = max(0, min(self.wscroll, max(0, len(lines) - body_h)))
        for i, (s, a, k) in enumerate(lines[self.wscroll:self.wscroll + body_h]):
            if k is not None and k == self.asel and self.wfocus == "agents":
                a = curses.A_REVERSE
            self.put(y + i, 0, s, a, width=w - 1)
        focus = "↑↓ 选代理  Enter 代理的任务详情  ← 回工作流列表" if self.wfocus == "agents" else "↑↓ 选工作流  Enter 工作流详情  → 选代理"
        self.footer(h, w, f" {focus}  v 最近几步（{self.wverbose or '关'}）  C 取消  R 续跑  PgUp/PgDn 翻  4 帮助  q 退出 ")
        self.scr.refresh()

    def draw_tasks(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        info = core.daemon_info()
        counts = {}
        tok = 0
        for t in core.list_tasks() if self.only_attention else self.tasks:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        for t in self.tasks:
            u = t.get("usage") or {}
            tok += u.get("in", 0) + u.get("out", 0)
        up = "● 后台运行中" if info and info["alive"] else "○ 后台没在运行：按 U 启动"
        parts = [f"{core.STATUS_ZH[k]} {counts[k]}" for k in ("running", "queued", "waiting", "review", "failed", "paused") if counts.get(k)]
        head = f" crew  {up}  │ " + " · ".join(parts or ["没有任务"]) + f"  │ 24h tokens {core.fmt_tokens(tok)}" + ("  │ 只看需处理" if self.only_attention else "")
        self.put(0, 0, core.pad(head, w), curses.A_REVERSE)
        self.tabs(1, w)
        self.scr.move(0, 0)
        R = 1               # 页签占一行，下面整体下移

        list_h = max(3, min(len(self.tasks) + 1, (h - 5) * 2 // 5))
        cols = [("", 2), ("ID", 16), ("状态", 8), ("轮", 3), ("回合", 9), ("tokens", 8), ("用时", 8), ("标题 / 备注", 0)]
        x = 0
        for name, cw in cols:
            self.put(1 + R, x, name, curses.A_BOLD)
            x += cw
        top = max(0, self.sel - (list_h - 2))
        for i, t in enumerate(self.tasks[top:top + list_h - 1]):
            y = 2 + R + i
            idx = top + i
            st = t["status"]
            attr = curses.A_REVERSE if idx == self.sel else 0
            u = t.get("usage") or {}
            cells = [
                (GLYPH.get(st, " ") + " ", 2, self.color(st)),
                (t["id"], 16, 0),
                (core.STATUS_ZH.get(st, st), 8, self.color(st) | curses.A_BOLD),
                (str(t.get("round", 1)), 3, 0),
                (f"{t.get('turns', 0)}回合", 9, 0),
                (core.fmt_tokens(u.get("in", 0) + u.get("out", 0)), 8, 0),
                (core.fmt_secs(core.elapsed(t)), 8, 0),
            ]
            x = 0
            if attr:
                self.put(y, 0, " " * w, attr)
            for text, cw, a in cells:
                self.put(y, x, core.pad(text, cw), a | attr)
                x += cw
            extra = t["title"]
            if st == "waiting" and t.get("question"):
                extra += f"  ❓{t['question']['text']}"
            elif t.get("note") and st not in ("running",):
                extra += f"  · {t['note']}"
            extra += f"  〔{core.origin_label(t)}〕"
            self.put(y, x, core.pad(extra, max(0, w - x)), attr)
        if not self.tasks:
            self.put(2 + R, 2, "没有任务。按 n 派一个，或让 Claude 用 crew 工具派发。", curses.A_DIM)

        y0 = 2 + R + list_h - 1
        t = self.current()
        title = f" {t['id']} · {t['title']} · {core.STATUS_ZH.get(t['status'])} · 第 {t.get('round', 1)} 轮 · {core.origin_label(t, 60)} · {t.get('profile')} · {t['cwd']} " if t else " "
        tab = "[时间线]  成果 " if self.view == "log" else " 时间线  [成果]"
        self.put(y0, 0, "─" * w, curses.A_DIM)
        self.put(y0, 2, title, curses.A_BOLD, width=w - 22)
        self.put(y0, max(0, w - 18), tab, curses.A_DIM)
        body_h = h - y0 - 2
        if t:
            lines = self.detail_lines(t, w - 1)
            if self.view == "log":
                end = len(lines) - self.scroll
                start = max(0, end - body_h)
                view = lines[start:end]
            else:
                start = self.scroll
                view = lines[start:start + body_h]
            for i, (s, a) in enumerate(view):
                self.put(y0 + 1 + i, 0, s, a, width=w - 1)
            if self.view == "log" and self.scroll:
                self.put(y0, max(0, w - 34), f" ↑ 上翻 {self.scroll} 行，End 跟随 ", curses.color_pair(3))
        foot = " ↑↓ 选择  Enter 任务详情  ←→ 时间线/成果  m 插话  a 回答  v 验收  p 暂停/继续  c 取消  x 重试  4 帮助  q 退出 "
        if self.flash and time.time() - self.flash_t < 6:
            foot = " " + self.flash + " "
        self.put(h - 1, 0, core.pad(foot, w - 1), curses.A_REVERSE)
        self.scr.refresh()

    def detail_lines(self, t, w):
        out = []
        if self.view == "log":
            for e in core.read_events(t["id"], tail=400):
                k = e["k"]
                a = curses.A_DIM if k in ("turn", "out", "compact") else 0
                if k in ("ask", "review"):
                    a = curses.color_pair(5) if k == "ask" else curses.color_pair(3)
                elif k == "submit":
                    a = curses.color_pair(6) | curses.A_BOLD
                elif k == "status":
                    a = curses.A_BOLD
                elif k == "out" and e.get("code") not in (0, None):
                    a = curses.color_pair(1)
                elif k == "say":
                    a = curses.color_pair(4)
                for i, chunk in enumerate(wrap(cli.fmt_event(e), w, 3 if k in ("say", "submit", "ask", "review", "steer", "answer") else 1)):
                    out.append((chunk, a))
            return out
        res = core.read_json(os.path.join(core.task_dir(t["id"]), "result.json"))
        sec = lambda s: out.append((s, curses.A_BOLD | curses.color_pair(4)))  # noqa: E731
        body = lambda s, a=0: [out.append((x, a)) for para in str(s).split("\n") for x in wrap(para, w, 999)]  # noqa: E731
        if t.get("question"):
            sec("── 待回答的提问（按 a 回答）")
            body(t["question"]["text"], curses.color_pair(5))
            if t["question"].get("options"):
                body("选项：" + " / ".join(t["question"]["options"]))
        if res:
            sec(f"── 成果（第 {res.get('round')} 轮，{core.stamp(res.get('t'))}）")
            body(res.get("summary", ""))
            if res.get("files"):
                sec("── 文件")
                for f in res["files"]:
                    body("  " + f)
            if res.get("self_check"):
                sec("── 自查")
                body(res["self_check"])
            if res.get("open_issues"):
                sec("── 未解决 / 假设")
                for x in res["open_issues"]:
                    body("  - " + x)
            if res.get("data") is not None:
                sec("── 数据")
                body(json.dumps(res["data"], ensure_ascii=False, indent=1)[:8000], curses.A_DIM)
        else:
            out.append(("（还没有提交成果）", curses.A_DIM))
        for r in t.get("reviews") or []:
            sec(f"── 验收 第 {r.get('round')} 轮 · {r.get('by')} · {r['verdict']} {r.get('score') or ''}")
            body(r.get("text", ""))
        sec("── 派发来源")
        body(cli.origin_text(t, fresh=False))     # 每半秒重画一次：不去重读对话记录
        sec("── 任务")
        body(t["prompt"])
        if t.get("accept"):
            sec("── 验收标准")
            body(t["accept"])
        return out

    # ───────── 输入
    def prompt(self, label, initial=""):
        """底部单行输入，支持中文。Enter 确认，Esc 取消，Ctrl+U 清空。"""
        buf = list(initial)
        curses.curs_set(1)
        self.scr.timeout(-1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                text = "".join(buf)
                shown = text
                while core.swidth(label) + core.swidth(shown) > w - 2 and shown:
                    shown = shown[1:]
                self.put(h - 1, 0, core.pad(label + shown, w - 1), curses.A_REVERSE)
                try:
                    self.scr.move(h - 1, min(w - 2, core.swidth(label) + core.swidth(shown)))
                except curses.error:
                    pass
                self.scr.refresh()
                ch = self.scr.get_wch()
                if ch in ("\n", "\r", curses.KEY_ENTER):
                    return text.strip()
                if ch == "\x1b":
                    return None
                if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if buf:
                        buf.pop()
                elif ch == "\x15":
                    buf = []
                elif isinstance(ch, str) and ch.isprintable():
                    buf.append(ch)
        finally:
            curses.curs_set(0)
            self.scr.timeout(500)

    def choose(self, label, options):
        h, w = self.scr.getmaxyx()
        self.put(h - 1, 0, core.pad(label + "  " + "  ".join(f"{i + 1} {o}" for i, o in enumerate(options)) + "   Esc 取消", w - 1), curses.A_REVERSE | curses.color_pair(3))
        self.scr.refresh()
        self.scr.timeout(-1)
        try:
            while True:
                ch = self.scr.get_wch()
                if ch == "\x1b":
                    return None
                if isinstance(ch, str) and ch.isdigit() and 1 <= int(ch) <= len(options):
                    return int(ch) - 1
        finally:
            self.scr.timeout(500)

    def act(self, ch):
        if ch in ("q", "Q"):
            return False
        if self.page in ("conv", "wfd"):
            r = (self.act_conv if self.page == "conv" else self.act_wfd)(ch)
            if r is not None:
                return r
        if ch in ("1", "2", "3", "4"):
            self.stack, self.conv_prev = [], []
        if ch == "4":
            if self.page != "help":
                self.back, self.page, self.hscroll = self.page, "help", 0
            return True
        if self.page == "help":
            if ch in (curses.KEY_UP, "k", curses.KEY_PPAGE):
                self.hscroll = max(0, self.hscroll - (10 if ch == curses.KEY_PPAGE else 1))
            elif ch in (curses.KEY_DOWN, "j", curses.KEY_NPAGE, " "):
                self.hscroll += 10 if ch in (curses.KEY_NPAGE, " ") else 1
            elif ch == "\x1b":
                self.page = getattr(self, "back", "home")
                return True
            if ch not in ("1", "2", "3"):
                return True
        if ch in ("1", "2", "3"):
            self.page = {"1": "home", "2": "wf", "3": "tasks"}[ch]
            if ch == "3":
                self.conv = self.jump = ""
            self.wscroll = 0
            return True
        if ch == "\x1b":
            if self.page == "tasks" and (self.conv or self.jump):
                self.conv = self.jump = ""
            else:
                self.page = "home"
            return True
        if ch == "U":
            cli.ensure_up()
            self.say("后台服务已启动")
            return True
        if self.page == "home":
            return self.act_home(ch)
        if self.page == "wf":
            return self.act_wf(ch)
        return self.act_tasks(ch)

    def act_home(self, ch):
        if ch in (curses.KEY_UP, "k"):
            self.hsel = max(0, self.hsel - 1)
        elif ch in (curses.KEY_DOWN, "j"):
            self.hsel = min(len(self.home_items) - 1, self.hsel + 1)
        elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT) and self.home_items:
            # ←→ 跳到上一块 / 下一块（工作流 · 对话 · 等处理的任务）的第一项
            kinds = [it[0] for it in self.home_items]
            cur = kinds[self.hsel]
            if ch == curses.KEY_RIGHT:
                nxt = next((i for i in range(self.hsel + 1, len(kinds)) if kinds[i] != cur), self.hsel)
            else:
                start = next((i for i in range(self.hsel, -1, -1) if kinds[i] != cur), None)
                nxt = 0 if start is None else next(i for i in range(start + 1) if kinds[i] == kinds[start])
            self.hsel = nxt
        elif ch in ("\n", "\r", curses.KEY_ENTER) and self.home_items:
            kind, key, _ = self.home_items[self.hsel]
            if kind == "wf":
                self.open_wfd(key)
            elif kind == "task":
                self.open_conv(key)
            elif kind == "conv":
                self.page, self.conv, self.sel = "tasks", key, 0
            else:
                self.page, self.conv = "tasks", ""
                self.load(force=True)
                ids = [t["id"] for t in self.tasks]
                self.sel = ids.index(key) if key in ids else 0
        return True

    def act_wf(self, ch):
        cur = self.wfs[self.wsel] if self.wfs else None
        if ch in ("\t", curses.KEY_LEFT, curses.KEY_RIGHT):
            # ←→ / Tab：在工作流列表和下面的代理之间切换
            new = "list" if ch == curses.KEY_LEFT else "agents" if ch == curses.KEY_RIGHT else ("agents" if self.wfocus == "list" else "list")
            if new != self.wfocus:
                self.wfocus, self.asel = new, 0
        elif ch in ("\n", "\r", curses.KEY_ENTER) and self.wfocus == "list" and cur:
            self.open_wfd(cur["id"])
        elif ch in (curses.KEY_UP, "k"):
            if self.wfocus == "list":
                self.wsel, self.wscroll = max(0, self.wsel - 1), 0
            else:
                self.asel = max(0, self.asel - 1)
        elif ch in (curses.KEY_DOWN, "j"):
            if self.wfocus == "list":
                self.wsel, self.wscroll = min(len(self.wfs) - 1, self.wsel + 1), 0
            else:
                self.asel = min(len(getattr(self, "_agents", [])) - 1, self.asel + 1)
        elif ch == curses.KEY_PPAGE:
            self.wscroll = max(0, self.wscroll - self.scr.getmaxyx()[0] // 2)
        elif ch == curses.KEY_NPAGE:
            self.wscroll += self.scr.getmaxyx()[0] // 2
        elif ch == "v":
            self.wverbose = {0: 3, 3: 8, 8: 0}[self.wverbose]
        elif ch in ("\n", "\r", curses.KEY_ENTER) and self.wfocus == "agents":
            ag = getattr(self, "_agents", [])
            if ag and ag[self.asel].get("task"):
                self.open_conv(ag[self.asel]["task"])
            else:
                self.say("这个代理还没开始（排队中）")
        elif ch in ("C", "R") and cur:
            return self.wf_ctl(ch, cur)
        return True

    def wf_ctl(self, ch, cur):
        if ch == "C" and cur:
            if cur["status"] in wf.DONE:
                self.say("已经结束了")
            elif self.choose(f"取消工作流 {cur['name']}（在跑的代理一并取消）？", ["取消", "不要"]) == 0:
                open(os.path.join(wf.wdir(cur["id"]), "ctl", "cancel"), "w").close()
                self.say("已发出取消")
        elif ch == "R" and cur:
            if cur["status"] not in wf.DONE and wf.pid_alive((cur.get("proc") or {}).get("pid")):
                self.say("正在运行，不用续跑")
            else:
                open(os.path.join(wf.wdir(cur["id"]), "ctl", "start"), "w").close()
                cli.ensure_up()
                self.say("已请求续跑：已完成的代理直接复用结果")
        return True

    def act_tasks(self, ch, t=None):
        t = t or self.current()
        if ch in ("\n", "\r", curses.KEY_ENTER):
            self.open_conv()
        elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT):
            self.view, self.scroll = ("log" if ch == curses.KEY_LEFT else "result"), 0
        elif ch in (curses.KEY_UP, "k"):
            self.sel, self.scroll = max(0, self.sel - 1), 0
        elif ch in (curses.KEY_DOWN, "j"):
            self.sel, self.scroll = min(len(self.tasks) - 1, self.sel + 1), 0
        elif ch == "\t":
            self.view, self.scroll = ("result" if self.view == "log" else "log"), 0
        elif ch == curses.KEY_PPAGE:
            self.scroll += max(1, self.scr.getmaxyx()[0] // 2) if self.view == "log" else 0
            if self.view == "result":
                self.scroll = max(0, self.scroll - self.scr.getmaxyx()[0] // 2)
        elif ch == curses.KEY_NPAGE:
            if self.view == "log":
                self.scroll = max(0, self.scroll - self.scr.getmaxyx()[0] // 2)
            else:
                self.scroll += self.scr.getmaxyx()[0] // 2
        elif ch == curses.KEY_END:
            self.scroll = 0
        elif ch == "h":
            self.only_attention = not self.only_attention
            self.load(force=True)
        elif ch == "n":
            text = self.prompt("新任务（在当前目录执行；详细任务建议让 Claude 派）> ")
            if text:
                nt = cli.save_new_task(cli.make_task({"prompt": text}, core.config(), "你"))
                cli.ensure_up()
                self.say(f"已派发 {nt['id']}")
                self.load(force=True)
        elif t is None:
            pass
        elif ch == "m":
            text = self.prompt(f"插话给 {t['id']} > ")
            if text:
                core.drop(t["id"], "steer", text=text, frm="你")
                self.say("已投递，工人下一回合读到")
        elif ch == "a":
            if t["status"] != "waiting":
                self.say("这个任务没有待回答的提问")
            else:
                q = t.get("question") or {}
                opts = q.get("options") or []
                text = None
                if opts:
                    i = self.choose(f"回答：{core.clip(q.get('text', ''), 40)}", opts + ["自己写"])
                    if i is not None and i < len(opts):
                        text = opts[i]
                if text is None:
                    text = self.prompt("回答 > ")
                if text:
                    core.drop(t["id"], "answer", text=text, frm="你")
                    self.say("已回答")
        elif ch == "v":
            if t["status"] not in ("review", "failed"):
                self.say("只有待验收或失败的任务能验收")
            else:
                i = self.choose(f"验收 {t['id']}：", ["通过", "退回修改", "否决"])
                if i is not None:
                    verdict = ["accept", "revise", "reject"][i]
                    text = self.prompt("意见（退回必填，写具体）> ") if verdict != "accept" else (self.prompt("备注（可空）> ") or "")
                    if verdict == "revise" and not text:
                        self.say("退回修改必须写意见，已取消")
                    elif text is not None:
                        core.drop(t["id"], "review", verdict=verdict, text=text, frm="你")
                        self.say(f"已验收：{['通过', '退回修改', '否决'][i]}")
        elif ch == "p":
            kind = "resume" if t["status"] == "paused" else "pause"
            core.drop(t["id"], kind, frm="你")
            self.say("已投递" + ("继续" if kind == "resume" else "暂停（当前回合结束后生效，正在跑的命令会被终止）"))
        elif ch == "c":
            if self.choose(f"确定取消 {t['id']}？", ["取消任务", "不要"]) == 0:
                core.drop(t["id"], "cancel", frm="你")
                self.say("已投递取消")
        elif ch == "D":
            if t["status"] != "failed":
                self.say("只有失败的任务能在这里删除")
            elif self.choose(f"删除 {t['id']}（任务目录、对话记录、成果一起删，不能恢复）？", ["删除", "不要"]) == 0:
                import shutil
                shutil.rmtree(core.task_dir(t["id"]), ignore_errors=True)
                self.say(f"已删除 {t['id']}")
        elif ch == "x":
            if t["status"] != "failed":
                self.say("只有失败的任务能重试")
            else:
                core.drop(t["id"], "resume", retry=True, frm="你")
                self.say("已投递重试：从断点续跑")
        return True

    # ───────── 详情页：整页只有这一项。task = 任务（派活、每一回合、成果），wfd = 工作流（全部阶段、代理、日志、返回值）
    def push(self, page):
        self.stack = getattr(self, "stack", [])
        if self.page not in ("conv", "wfd") or self.stack[-1:] != [self.page]:
            self.stack.append(self.page)
        self.page = page

    def go_back(self):
        st = getattr(self, "stack", [])
        self.page = st.pop() if st else "home"
        if self.page == "conv":
            self.conv_tid = self.conv_prev.pop() if getattr(self, "conv_prev", None) else self.conv_tid

    def open_conv(self, tid=None):
        tid = tid or (self.current() or {}).get("id")
        if not tid:
            return
        if self.page == "conv":
            self.conv_prev = getattr(self, "conv_prev", []) + [self.conv_tid]
        t = self.conv_task(tid)
        live = t.get("status") not in core.FINAL and t.get("status") not in core.NEEDS_LEAD
        self.conv_tid, self.ctop, self.cfollow, self._cconv = tid, 0, live, None
        self.push("conv")

    def open_wfd(self, wid):
        self.wfd_id, self.dtop, self.dsel, self._dcache = wid, 0, 0, None
        self.push("wfd")

    def conv_task(self, tid=None):
        tid = tid or self.conv_tid
        return next((x for x in self.all_tasks if x["id"] == tid), None) or core.load_task(tid) or {"id": tid, "status": "?"}

    def conv_lines(self, w):
        """任务详情渲染成 [(文字, 属性)]；时间线、成果文件没变，宽度和开关没变就用缓存。"""
        t = core.load_task(self.conv_tid) or self.conv_task()
        d = core.task_dir(self.conv_tid)

        def size(f):
            try:
                return os.path.getsize(os.path.join(d, f))
            except OSError:
                return 0
        key = (self.conv_tid, size("events.jsonl"), size("result.json"), w, getattr(self, "show_think", True), getattr(self, "full_out", False), t.get("status"))
        if getattr(self, "_cconv", None) and self._cconv[0] == key:
            return self._cconv[1]
        show_think, full_out = key[4], key[5]
        Y, B, C, G, R, M = (curses.color_pair(i) for i in (3, 4, 6, 2, 1, 5))
        DIM, BOLD = curses.A_DIM, curses.A_BOLD
        L = []
        body = lambda text, attr=0, indent=2, cap=None, more="": add_body(L, w, text, attr, indent, cap, more)  # noqa: E731

        # 任务信息
        u = t.get("usage") or {}
        L.append(("任务信息", Y | BOLD))
        kv = [("ID", t["id"]), ("标题", t.get("title") or ""), ("状态", core.STATUS_ZH.get(t.get("status"), t.get("status"))
                                                                   + (f"（{t['note']}）" if t.get("note") else "")),
              ("轮次", f"第 {t.get('round') or 1} 轮 / 最多 {t.get('max_rounds') or '-'} 轮 · {t.get('turns') or 0} 回合" + (f"（上限 {t['max_turns']}）" if t.get("max_turns") else "")),
              ("token", f"入 {core.fmt_tokens(u.get('in'))}（其中缓存 {core.fmt_tokens(u.get('cached'))}）· 出 {core.fmt_tokens(u.get('out'))} · 合计 {core.fmt_tokens((u.get('in') or 0) + (u.get('out') or 0))}"),
              ("时间", " · ".join(x for x in (f"派发 {utc(t.get('created'))}" if t.get("created") else "", f"开始 {utc(t.get('started'))}" if t.get("started") else "",
                                               f"结束 {utc(t.get('finished'))}" if t.get("finished") else "", f"最近更新 {utc(t.get('updated'))}" if t.get("updated") else "") if x)),
              ("派发方", f"{core.origin_label(t)} · owner {t.get('owner') or '-'}" + (f" · 批次 {t['batch']}" if t.get("batch") else "") + (f" · 分组 {t['group']}" if t.get("group") else "")),
              ("模型", t.get("model_used") or t.get("profile") or "-"), ("目录", t.get("cwd") or "-")]
        if t.get("activity") and t.get("status") == "running":
            kv.append(("当前", t["activity"]))
        for k_, v_ in kv:
            body(f"{k_}：{v_}", 0, 2)
        if t.get("files"):
            body("材料：", 0, 2)
            for f in t["files"]:
                body(f, DIM, 6)
        if t.get("accept"):
            body("验收标准：", 0, 2)
            body(t["accept"], DIM, 6)
        if t.get("question") and t.get("status") == "waiting":
            body("待回答的提问：" + str((t["question"] or {}).get("text", "")), M | BOLD, 2)

        # 全部往来
        for b in cli.convo_blocks(t):
            k, when = b["kind"], (core.stamp(b["t"]) if b.get("t") else "")
            if k == "turn":
                lab = f"── {b['head']} · {b['text']} · {when} "
                L.append(("", 0))
                L.append((lab + "─" * max(0, w - 2 - core.swidth(lab)), DIM))
            elif k == "lead":
                L.append(("", 0))
                L.append((f"■ {b['who']} · {b['head']}  {when}", Y | BOLD))
                body(b["text"])
            elif k == "think":
                if show_think:
                    L.append(("  ∴ 推理", M | DIM))
                    body(b["text"], DIM, indent=4)
                else:
                    L.append((f"  ∴ 推理（{len(b['text'])} 字，按 r 显示）", M | DIM))
            elif k == "say":
                L.append((f"● 工人  {when}", B | BOLD))
                body(b["text"])
            elif k == "tool":
                txt = str(b["text"])
                if "\n" not in txt and core.swidth(txt) <= w - core.swidth(b["head"]) - 8:
                    L.append((f"  ▶ {b['head']}  {txt}", C))
                else:
                    L.append((f"  ▶ {b['head']}", C))
                    body(txt, C, indent=6, cap=None if full_out else 12, more="（o 展开）")
            elif k == "out":
                L.append((f"  ◀ {b['head']}", G if b.get("ok") else R))
                if b["text"].strip():
                    body(b["text"], DIM, indent=6, cap=None if full_out else 6, more="（o 展开）")
            elif k == "ask":
                L.append(("", 0))
                L.append((f"? 工人 · 提问  {when}", M | BOLD))
                body(b["text"])
            elif k == "submit":
                L.append(("", 0))
                L.append((f"◆ 工人 · {b['head']}  {when}", G | BOLD))
                body(b["text"])
            else:
                L.append((f"· crew · {b['head']}  {when}  " + core.clip(b.get("text") or "", w - 30), R if b.get("bad") else DIM))

        # 最新成果（完整）
        res = core.read_json(os.path.join(d, "result.json"))
        if res:
            L.append(("", 0))
            L.append((f"成果（第 {res.get('round') or t.get('round') or 1} 轮，完整）", G | BOLD))
            body(res.get("summary") or "")
            if res.get("self_check"):
                body("自查：", BOLD, 2)
                body(res["self_check"], 0, 6)
            if res.get("open_issues"):
                body(f"未解决（{len(res['open_issues'])}）：", BOLD, 2)
                for x in res["open_issues"]:
                    body("· " + str(x), 0, 6)
            if res.get("files"):
                body(f"文件（{len(res['files'])}）：", BOLD, 2)
                for f in res["files"]:
                    try:
                        sz = core.fmt_bytes(os.path.getsize(f)) if hasattr(core, "fmt_bytes") else f"{os.path.getsize(f) // 1024}KB"
                    except OSError:
                        sz = "不存在"
                    body(f"{f}  （{sz}）", 0, 6)
            if res.get("data") not in (None, {}, []):
                body("data：", BOLD, 2)
                body(json.dumps(res["data"], ensure_ascii=False, indent=1), DIM, 6, cap=None if full_out else 40, more="（o 展开）")
        self._cconv = (key, L)
        return L

    def wfd_lines(self, w):
        """工作流详情：[(文字, 属性, 代理序号或 None)]。"""
        x = wf.load(self.wfd_id) or {}
        steps = getattr(self, "dsteps", 5)
        tr = wf.tree(x, steps=steps) if x else None
        Y, G, R, C = (curses.color_pair(i) for i in (3, 2, 1, 6))
        DIM, BOLD = curses.A_DIM, curses.A_BOLD
        L = []
        self._dagents = []
        if not tr:
            return [("找不到这个工作流", R, None)]
        r = tr["wf"]

        def body(text, attr=0, indent=2, cap=None, more=""):
            tmp = []
            add_body(tmp, w, text, attr, indent, cap, more)
            L.extend((a, b, None) for a, b in tmp)
        L.append(("工作流信息", Y | BOLD, None))
        cnt = r.get("counts") or {}
        for k_, v_ in (("ID", x["id"]), ("名称", x.get("name")), ("状态", wf.ZH.get(x["status"], x["status"]) + (f" · 第 {tr['attempt']} 次启动" if tr.get("attempt", 1) > 1 else "")),
                       ("说明", tr.get("description") or "-"), ("派发方", f"{r.get('origin')} · owner {x.get('owner') or '-'}"),
                       ("目录", tr.get("cwd") or "-"), ("脚本", os.path.join(wf.wdir(x["id"]), "script.js")),
                       ("时间", " · ".join(v for v in (f"创建 {utc(x.get('created'))}" if x.get("created") else "", f"开始 {utc(x.get('started'))}" if x.get("started") else "",
                                                        f"结束 {utc(x.get('finished'))}" if x.get("finished") else "", f"用时 {core.fmt_secs(r['elapsed'])}") if v)),
                       ("代理", f"{r['agents']} 个：" + " · ".join(f"{wf.AZH.get(k2, k2)} {v2}" for k2, v2 in cnt.items() if v2) + f" · token {core.fmt_tokens(r['tokens'])}")):
            body(f"{k_}：{v_}")
        if tr.get("args") not in (None, {}, []):
            body("参数：")
            body(json.dumps(tr["args"], ensure_ascii=False, indent=1), DIM, 6, cap=30, more="")
        L.append(("", 0, None))
        L.append((f"阶段与代理（每个代理最近 {steps} 步；s 切换 0/5/15）", Y | BOLD, None))
        k = 0
        for ph in tr["phases"]:
            g = {"done": "✓", "running": "▶", "pending": "·"}[ph["status"]]
            L.append((f"{g} {ph['title']}  {ph['done']}/{ph['total']}" + (f"  失败 {ph['failed']}" if ph["failed"] else "")
                      + (f"  {core.fmt_secs(ph['elapsed'])}" if ph["total"] else "") + (f"  — {ph['detail']}" if ph["detail"] else ""),
                      BOLD | (G if ph["status"] == "running" else 0), None))
            for a in ph["agents"]:
                st = a["status"]
                tag = (f"[{a['model']}] " if a.get("model") else "") + ("缓存 " if a["cached"] else "") + ("续上文 " if a.get("cont") else "") + (f"重试{a['retries']} " if a["retries"] else "")
                color = {"running": "running", "retry": "review", "failed": "failed", "done": "accepted", "cancelled": "cancelled"}.get(st, "queued")
                L.append((f"   {wf.GLYPH.get(st, ' ')} {core.pad(str(a['n']), 4)}{core.pad(a['label'], 30)} {core.pad(wf.AZH.get(st, st), 5)}"
                          f"{core.pad(core.fmt_secs(a['elapsed']), 8)}{core.pad(str(a['turns'] or 0) + '回合', 8)}{core.pad(core.fmt_tokens(a['tokens']), 7)}{tag}"
                          + (a.get("task") or ""), self.color(color), k))
                self._dagents.append(a)
                if st in ("running", "queued", "retry", "paused") and a.get("step"):
                    body("当前：" + a["step"] + (f"（{core.fmt_secs(a['step_ago'])}前）" if a.get("step_ago") is not None else ""), C, 8)
                if a.get("summary") and st == "done":
                    body("结论：" + a["summary"], 0, 8)
                if a.get("note") and st in ("failed", "cancelled"):
                    body("原因：" + a["note"], R, 8)
                for s_ in a.get("recent") or []:
                    body(s_, DIM, 8)
                k += 1
        if x.get("logs"):
            L.append(("", 0, None))
            L.append((f"日志（{len(x['logs'])} 条）", Y | BOLD, None))
            for lg in x["logs"]:
                body(f"{core.stamp(lg['t'])} {lg['msg']}", DIM)
        if tr.get("error"):
            L.append(("", 0, None))
            L.append(("错误", R | BOLD, None))
            body(tr["error"], R)
        res = core.read_json(os.path.join(wf.wdir(x["id"]), "result.json"))
        if res is not None or tr.get("result_preview"):
            L.append(("", 0, None))
            L.append(("返回值（完整）", G | BOLD, None))
            val = res.get("value", res) if isinstance(res, dict) else res
            body(json.dumps(val, ensure_ascii=False, indent=1) if not isinstance(val, str) else val, 0, 2,
                 cap=None if getattr(self, "full_out", False) else 80, more="（o 展开）")
        return L

    def draw_detail(self, title, lines, top_attr, foot, sel=None):
        """整页画一份详情：标题行 + 滚动内容；sel = 要高亮、保持可见的行号。返回修正后的 top。"""
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        self.header(w)
        self.tabs(1, w)
        self.put(2, 1, core.clip(title, w - 2), curses.A_BOLD)
        H = h - 4
        top = getattr(self, top_attr)
        if sel is not None:
            if sel < top:
                top = max(0, sel - 2)
            elif sel >= top + H:
                top = sel - H + 3
        top = max(0, min(top, max(0, len(lines) - H)))
        for i, ln in enumerate(lines[top:top + H]):
            attr = curses.A_REVERSE if sel is not None and top + i == sel else ln[1]
            self.put(3 + i, 0, ln[0], attr, width=w - 1)
        pos = "末尾" if top + H >= len(lines) else f"{min(100, (top + H) * 100 // max(1, len(lines)))}%"
        self.footer(h, w, f"{foot}  [{pos}] ")
        setattr(self, top_attr, top)
        self.scr.refresh()

    def draw_conv(self):
        h, w = self.scr.getmaxyx()
        t = self.conv_task()
        lines = self.conv_lines(w)
        if self.cfollow:
            self.ctop = max(0, len(lines) - (h - 4))
        self.draw_detail(f"任务  {t['id']}  {t.get('title') or ''}  ·  {core.STATUS_ZH.get(t.get('status'), t.get('status'))}", lines, "ctop",
                         f" ↑↓ 滚动  PgUp/PgDn 翻页  Home 开头  End 最新  r 推理{'隐藏' if getattr(self, 'show_think', True) else '显示'}  "
                         f"o 输出{'收起' if getattr(self, 'full_out', False) else '展开'}  m 插话  a 回答  v 验收  Esc 返回")

    def draw_wfd(self):
        h, w = self.scr.getmaxyx()
        lines = self.wfd_lines(w)
        n = len(self._dagents)
        self.dsel = max(0, min(self.dsel, n - 1)) if n else 0
        at = next((i for i, l in enumerate(lines) if l[2] == self.dsel), None) if n else None
        x = wf.load(self.wfd_id) or {}
        self.draw_detail(f"工作流  {x.get('id', self.wfd_id)}  {x.get('name', '')}  ·  {wf.ZH.get(x.get('status'), x.get('status'))}", lines, "dtop",
                         " ↑↓ 选代理  Enter 打开代理的任务  PgUp/PgDn 翻页  Home/End  s 每代理步数  o 返回值展开  C 取消  R 续跑  Esc 返回", sel=at)

    def act_conv(self, ch):
        """任务详情的按键；返回 None 交给通用处理（1–4 跳页、q 退出）。"""
        H = self.scr.getmaxyx()[0] - 4
        n = len((getattr(self, "_cconv", None) or (None, []))[1])
        if ch in ("\x1b", curses.KEY_BACKSPACE, "\x7f"):
            self.go_back()
        elif ch in (curses.KEY_UP, "k"):
            self.cfollow, self.ctop = False, max(0, self.ctop - 1)
        elif ch in (curses.KEY_DOWN, "j"):
            self.ctop += 1
        elif ch in (curses.KEY_PPAGE, "b", curses.KEY_LEFT):
            self.cfollow, self.ctop = False, max(0, self.ctop - H + 2)
        elif ch in (curses.KEY_NPAGE, " ", curses.KEY_RIGHT):
            self.ctop += H - 2
        elif ch in (curses.KEY_HOME, "g"):
            self.cfollow, self.ctop = False, 0
        elif ch in (curses.KEY_END, "G"):
            self.cfollow = True
        elif ch == "r":
            self.show_think = not getattr(self, "show_think", True)
        elif ch == "o":
            self.full_out = not getattr(self, "full_out", False)
        elif ch in ("m", "a", "v", "p", "c", "x"):
            return self.act_tasks(ch, self.conv_task())
        else:
            return None
        if not self.cfollow and ch in (curses.KEY_DOWN, "j", curses.KEY_NPAGE, " ", curses.KEY_RIGHT) and self.ctop >= n - H:
            self.cfollow = True       # 翻到底就恢复跟随
        return True

    def act_wfd(self, ch):
        H = self.scr.getmaxyx()[0] - 4
        n = len(getattr(self, "_dagents", []))
        if ch in ("\x1b", curses.KEY_BACKSPACE, "\x7f"):
            self.go_back()
        elif ch in (curses.KEY_UP, "k"):
            self.dsel = max(0, self.dsel - 1)
        elif ch in (curses.KEY_DOWN, "j"):
            self.dsel = min(max(0, n - 1), self.dsel + 1)
        elif ch in (curses.KEY_PPAGE, curses.KEY_LEFT):
            self.dtop = max(0, self.dtop - H + 2)
            self.dsel = max(0, self.dsel - 5)
        elif ch in (curses.KEY_NPAGE, " ", curses.KEY_RIGHT):
            self.dtop += H - 2
            self.dsel = min(max(0, n - 1), self.dsel + 5)
        elif ch in (curses.KEY_HOME, "g"):
            self.dtop, self.dsel = 0, 0
        elif ch in (curses.KEY_END, "G"):
            self.dtop, self.dsel = 10 ** 9, max(0, n - 1)
        elif ch == "s":
            self.dsteps = {0: 5, 5: 15, 15: 0}[getattr(self, "dsteps", 5)]
        elif ch == "o":
            self.full_out = not getattr(self, "full_out", False)
        elif ch in ("\n", "\r", curses.KEY_ENTER):
            ag = getattr(self, "_dagents", [])
            if ag and ag[self.dsel].get("task"):
                self.open_conv(ag[self.dsel]["task"])
            else:
                self.say("这个代理还没开始（排队中）")
        elif ch in ("C", "R"):
            x = wf.load(self.wfd_id)
            return self.wf_ctl(ch, x) if x else True
        else:
            return None
        return True

    def escape(self):
        """方向键没被 curses 认出来时（终端描述库缺失，或 SSH 延迟让 ESC 和后面的 [A 分两次到）会以原始序列到达：
        自己解析。第一个字符多等一会儿（200ms，网络慢也等得到），之后每个 60ms；只有真的没有后续才算 Esc 键，
        认不出的序列丢掉，不会被当成 Esc 把你踢回主页。"""
        seq = ""
        try:
            while len(seq) < 6:
                self.scr.timeout(200 if not seq else 60)
                try:
                    c = self.scr.get_wch()
                except curses.error:
                    break
                if not isinstance(c, str):
                    break
                seq += c
                if seq[-1].isalpha() or seq[-1] == "~":
                    break
        finally:
            self.scr.timeout(500)
        keys = {"[A": curses.KEY_UP, "OA": curses.KEY_UP, "[B": curses.KEY_DOWN, "OB": curses.KEY_DOWN, "[C": curses.KEY_RIGHT,
                "OC": curses.KEY_RIGHT, "[D": curses.KEY_LEFT, "OD": curses.KEY_LEFT, "[5~": curses.KEY_PPAGE, "[6~": curses.KEY_NPAGE,
                "[F": curses.KEY_END, "OF": curses.KEY_END, "[4~": curses.KEY_END, "[H": curses.KEY_HOME}
        return keys.get(seq, "\x1b" if not seq else "")

    def loop(self):
        while True:
            self.load()
            self.draw()
            try:
                ch = self.scr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                return
            if ch == curses.KEY_RESIZE:
                continue
            if ch == "\x1b":
                ch = self.escape()
            if not self.act(ch):
                return
            self.load(force=True)


def conv_key(t):
    return (t.get("origin") or {}).get("session") or t.get("owner") or ""


def conv_name(t):
    return core.origin_label(t).split(" › ")[0]


def utc(t):
    return time.strftime("%m-%d %H:%M:%S UTC", time.gmtime(t)) if t else "-"


def add_body(L, w, text, attr=0, indent=2, cap=None, more=""):
    """把一段文字按宽度折行加进 L（[(行, 属性)]）；cap 限制行数，多出的写“还有 N 行”。"""
    rows = []
    for para in str(text).replace("\t", "  ").split("\n"):
        rows += wrap_text(para, w - indent - 2) or [""]
    if cap and len(rows) > cap:
        hidden = len(rows) - cap
        L.extend((" " * indent + r, attr) for r in rows[:cap])
        L.append((" " * indent + f"… 还有 {hidden} 行{more}", curses.A_DIM))
        return
    L.extend((" " * indent + r, attr) for r in rows)


def wrap_text(s, width):
    """按显示宽度折成多行（中文算两格）。"""
    width = max(10, width)
    out, cur, cw = [], [], 0
    for c in s:
        x = core.cwidth(c)
        if cw + x > width:
            out.append("".join(cur))
            cur, cw = [], 0
        cur.append(c)
        cw += x
    if cur or not out:
        out.append("".join(cur))
    return out


def wrap(s, w, max_lines):
    """按显示宽度折行，最多 max_lines 行。"""
    indent = " " * 9 if max_lines < 999 else ""
    out, cur, cw = [], [], 0
    for c in s.replace("\t", "  "):
        lim = w if not out else w - len(indent)
        if c == "\n":
            out.append("".join(cur))
            cur, cw = [], 0
            continue
        x = core.cwidth(c)
        if cw + x > lim:
            out.append("".join(cur))
            cur, cw = [], 0
        cur.append(c)
        cw += x
    out.append("".join(cur))
    if len(out) > max_lines:
        out = out[:max_lines]
        out[-1] = core.clip(out[-1] + "…", w - len(indent))
    return [(indent + x if i else x) for i, x in enumerate(out)]


def run(scr):
    curses.curs_set(0)
    curses.use_default_colors()
    for n, c in ((1, curses.COLOR_RED), (2, curses.COLOR_GREEN), (3, curses.COLOR_YELLOW), (4, curses.COLOR_BLUE), (5, curses.COLOR_MAGENTA), (6, curses.COLOR_CYAN)):
        curses.init_pair(n, c, -1)
    scr.timeout(500)
    scr.keypad(True)
    if hasattr(curses, "set_escdelay"):
        curses.set_escdelay(200)
    App(scr).loop()


def main(a=None):
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        # 环境里的区域设置本机没装：用 C.UTF-8（中文照样显示）
        for name in ("C.UTF-8", "C.utf8"):
            try:
                locale.setlocale(locale.LC_ALL, name)
                break
            except locale.Error:
                continue
    if "utf" not in (locale.getpreferredencoding(False) or "").lower():
        for loc in ("C.UTF-8", "en_US.UTF-8", "zh_CN.UTF-8"):
            try:
                locale.setlocale(locale.LC_ALL, loc)
                break
            except locale.Error:
                pass
        else:
            print("警告：没有 UTF-8 区域设置，中文会显示成乱码（设置 LANG=C.UTF-8 再试）", file=__import__("sys").stderr)
            time.sleep(1.5)
    os.environ["ESCDELAY"] = "200"      # 方向键是 ESC [ A 三个字符，SSH 上可能分开到：等够 200ms 再判定是不是单独的 Esc
    try:
        curses.setupterm()
    except curses.error:
        # 精简容器里没有终端描述库：借用宿主机的，或者退回 xterm
        for d in ("/host/lib/terminfo", "/host/usr/share/terminfo", "/usr/share/terminfo", "/lib/terminfo"):
            if os.path.isdir(d):
                os.environ["TERMINFO"] = d
                try:
                    curses.setupterm()
                    break
                except curses.error:
                    os.environ["TERM"] = "xterm"
                    try:
                        curses.setupterm()
                        break
                    except curses.error:
                        continue
    curses.wrapper(run)
