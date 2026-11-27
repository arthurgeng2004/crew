"""工人用的 MCP：配置里的数据服务（mcp_servers）。

连接默认走 crew 自带的 mcp_bridge.py（streamable HTTP、会话握手）；配置 mcp_bridge 可以换成自己的桥脚本。
这里只做三件事：工具清单（带缓存）、按需给工人看工具说明、调用前按 schema 校验参数——
参数名写错、少了必填的，直接把正确的签名还给工人，不浪费一次真实调用。
"""
import importlib.util
import base64
import json
import os
import re
import threading
import time

from . import core


def purpose_of(s):
    """各服务一句话用途（给工人挑服务用；工具名从服务实时取）：配置 mcp_servers.<名字>.purpose"""
    c = (core.config().get("mcp_servers") or {}).get(s)
    return str(c.get("purpose") or "") if isinstance(c, dict) else ""


_bridge = None
_cache = {"t": 0, "servers": {}, "errors": {}}
_lock = threading.Lock()


def bridge(cfg):
    global _bridge
    if _bridge is None:
        path = cfg.get("mcp_bridge") or ""
        if not path:
            from . import mcp_bridge as m
            if not m.server_urls():
                raise RuntimeError("没有配置 MCP 服务（mcp_servers）")
            _bridge = m
            return m
        spec = importlib.util.spec_from_file_location("crew_mcp_bridge", os.path.expanduser(path))
        if not spec:
            raise RuntimeError(f"找不到 MCP 桥脚本：{path}（配置 mcp_bridge）")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _bridge = m
    return _bridge


def available(cfg):
    try:
        bridge(cfg)
        return True
    except Exception:  # noqa: BLE001
        return False


def catalog(cfg, max_age=600):
    """{server: [tool]}，tool 是 tools/list 的原样条目（含 inputSchema）。各服务并行取，10 分钟缓存。"""
    with _lock:
        if time.time() - _cache["t"] < max_age and _cache["servers"]:
            return _cache["servers"], _cache["errors"]
        b = bridge(cfg)
        servers, errors = {}, {}

        def one(s):
            j, err = b._rpc(s, "tools/list", {}, 30)
            if err or not isinstance(j, dict):
                errors[s] = err or "没有返回"
            else:
                servers[s] = (j.get("result") or {}).get("tools") or []
        ths = [threading.Thread(target=one, args=(s,), daemon=True) for s in sorted(b.server_urls())]
        for t in ths:
            t.start()
        for t in ths:
            t.join(40)
        _cache.update(t=time.time(), servers=servers, errors=errors)
        return servers, errors


def brief(cfg):
    """给工人开工时看的服务目录：一行一个服务（用途 + 工具名）。"""
    try:
        servers, errors = catalog(cfg)
    except Exception as e:  # noqa: BLE001
        return f"（MCP 服务目录取不到：{e}）"
    lines = []
    for s in sorted(set(servers) | set(errors)):
        if s in errors:
            lines.append(f"- {s}：现在连不上（{core.clip(errors[s], 80)}）")
        else:
            lines.append(f"- {s}：{purpose_of(s)}｜工具：{', '.join(t['name'] for t in servers[s])}")
    return "\n".join(lines)


def _ptype(p):
    t = p.get("type")
    if not t and p.get("anyOf"):
        t = "|".join(x.get("type", "?") for x in p["anyOf"] if x.get("type") != "null")
    if p.get("enum"):
        t = f"{t or ''} 取值 {'/'.join(map(str, p['enum'][:12]))}"
    return t or "any"


def signature(tool, full=False):
    sch = tool.get("inputSchema") or {}
    req = set(sch.get("required") or [])
    out = []
    for k, p in (sch.get("properties") or {}).items():
        d = " ".join(str(p.get("description") or "").split())
        dflt = f"，默认 {json.dumps(p['default'], ensure_ascii=False)}" if "default" in p else ""
        out.append(f"    {k}{'（必填）' if k in req else ''}: {_ptype(p)}{dflt}" + (f" — {d if full else core.clip(d, 160)}" if d else ""))
    return "\n".join(out) or "    （无参数）"


def describe(cfg, server=None, tool=None):
    servers, errors = catalog(cfg)
    if not server:
        return "可用的 MCP 服务（mcp_tools 带 server 看工具和参数）：\n" + brief(cfg)
    if server not in servers:
        return f"错误：没有服务 {server}" + (f"（连不上：{errors[server]}）" if server in errors else f"；现有：{', '.join(sorted(servers))}")
    ts = servers[server]
    if tool:
        t = next((x for x in ts if x["name"] == tool), None)
        if not t:
            return f"错误：{server} 没有工具 {tool}；有：{', '.join(x['name'] for x in ts)}"
        return f"{server}.{tool}\n{t.get('description') or ''}\n参数：\n{signature(t, full=True)}"
    parts = [f"{server}：{purpose_of(server)}"]
    for t in ts:
        d = " ".join(str(t.get("description") or "").split())
        parts.append(f"\n● {t['name']} — {core.clip(d, 400)}\n{signature(t)}")
    return "\n".join(parts) + "\n\n（某个工具的完整说明：mcp_tools 带 server 和 tool）"


def check_args(cfg, server, tool, args):
    """调用前校验：服务 / 工具存在、必填参数齐、没有不认识的参数名。返回错误文字（空 = 通过）。"""
    servers, errors = catalog(cfg)
    if server not in servers:
        if server in errors:
            return ""  # 清单取不到（服务可能刚坏）：照样去调，让真实错误回来
        return f"没有服务 {server}；现有：{', '.join(sorted(servers))}"
    t = next((x for x in servers[server] if x["name"] == tool), None)
    if not t:
        return f"{server} 没有工具 {tool}；有：{', '.join(x['name'] for x in servers[server])}"
    sch = t.get("inputSchema") or {}
    props = sch.get("properties") or {}
    missing = [k for k in sch.get("required") or [] if k not in args]
    unknown = [k for k in args if props and k not in props]
    if missing or unknown:
        return ((f"缺必填参数 {', '.join(missing)}；" if missing else "") + (f"不认识的参数 {', '.join(unknown)}；" if unknown else "")
                + f"{server}.{tool} 的参数：\n{signature(t)}")
    return ""


def call(cfg, server, tool, args, timeout=180, img_dir=None):
    """返回 (结果, 错误, 图片路径列表)。结果是解析好的 JSON 或原文。
    结果里的图片块（文档服务带的图表等）存成文件放进 img_dir，原位换成「[图 k：路径]」，
    工人用 view_image 看；不再把 base64 当文字塞进结果。"""
    b = bridge(cfg)
    if not img_dir or not hasattr(b, "_rpc"):
        d, err = b.call(server, tool, args or {}, timeout)
        return d, err, []
    j, err = b._rpc(server, "tools/call", {"name": tool, "arguments": args or {}}, timeout, None)
    if err:
        return None, err, []
    if not isinstance(j, dict) or j.get("error"):
        return None, json.dumps((j or {}).get("error"), ensure_ascii=False)[:300], []
    res = j.get("result") or {}
    parts, imgs = [], []
    for blk in res.get("content") or []:
        if isinstance(blk, dict) and blk.get("type") == "image" and blk.get("data"):
            os.makedirs(img_dir, exist_ok=True)
            ext = {"image/png": "png", "image/gif": "gif", "image/webp": "webp"}.get(blk.get("mimeType") or "", "jpg")
            # 服务翻页取图时每张图前有「[图 N] 前文」：文件按全文图号命名，和服务端对得上
            m = re.match(r"\[图 (\d+)\]", parts[-1] or "") if parts else None
            k = int(m.group(1)) if m else len(imgs) + 1
            p = os.path.join(img_dir, f"img_{k:03d}.{ext}")
            with open(p, "wb") as f:
                f.write(base64.b64decode(blk["data"]))
            imgs.append(p)
            parts.append(f"[图 {k}：{p}]")
        elif isinstance(blk, dict):
            parts.append(blk.get("text") if blk.get("type", "text") == "text" else json.dumps(blk, ensure_ascii=False))
        else:
            parts.append(str(blk))
    txt = "\n".join(p for p in parts if p)
    if res.get("isError"):
        return None, txt[:300] or "工具报错", imgs
    if isinstance(res.get("structuredContent"), (dict, list)) and not txt:
        return res["structuredContent"], None, imgs
    try:
        d = json.loads(txt)
    except ValueError:
        return txt, None, imgs
    if isinstance(d, dict) and d.get("error") and not any(k in d for k in ("data", "rows", "results", "items")):
        return None, str(d["error"])[:300], imgs
    return d, None, imgs
