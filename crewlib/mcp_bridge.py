"""chat 工人的 MCP 桥：只读取 config.json 的 mcp_servers；支持 stdio / Streamable HTTP。
Codex 工人使用自己的 CLI 配置，不经过这个桥。"""
import json
import atexit
import os
import re
import subprocess
import threading
import urllib.error
import urllib.request

from . import core

PROTOCOL = "2025-06-18"
_INSTRUCTIONS = {}
_SESSION = {}                     # HTTP 服务的 Mcp-Session-Id
_STDIO = {}                       # stdio 服务的子进程
_LOCK = threading.Lock()
_PLAIN = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 回环服务直接连接
_PROXIED = urllib.request.build_opener()


def secrets_env(cfg=None):
    """mcp_env_file 里的 KEY=VALUE（密钥），叠在当前环境变量上。"""
    cfg = cfg or core.config()
    env = dict(os.environ)
    path = os.path.expanduser(cfg.get("mcp_env_file") or os.path.join(core.HOME, "mcp.env"))
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass
    return env


def servers(cfg=None):
    """{名字: 配置}，只含启用的。"""
    cfg = cfg or core.config()
    out = {}
    for name, c in (cfg.get("mcp_servers") or {}).items():
        c = {"url": c} if isinstance(c, str) else dict(c or {})
        if c.get("url") or c.get("command"):
            out[name] = c
    for name, c in list(out.items()):
        if c.get("enabled") is False:
            out.pop(name)
            continue
    return out


def server_urls(cfg=None):
    """{名字: 地址或命令}（mcp.py 用它列服务）。"""
    return {k: v.get("url") or " ".join([v["command"]] + [str(a) for a in v.get("args") or []]) for k, v in servers(cfg).items()}


def description(name, cfg=None):
    return str((servers(cfg).get(name) or {}).get("description") or _INSTRUCTIONS.get(name) or "")


# ───────── streamable HTTP
def _headers(srv, env):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    h.update({k: str(v) for k, v in (srv.get("headers") or {}).items()})          # 非密钥的固定请求头
    for k, var in (srv.get("env_headers") or {}).items():
        if env.get(var):
            h[k] = env[var]
    if srv.get("bearer_token_env") and env.get(srv["bearer_token_env"]):
        h["Authorization"] = "Bearer " + env[srv["bearer_token_env"]]
    return h


def _post(srv, body, timeout, env, sid=None):
    h = _headers(srv, env)
    if sid:
        h["Mcp-Session-Id"] = sid
    url = srv["url"]
    local = re.match(r"https?://(127\.|localhost(?=[:/]|$))", url)
    r = (_PLAIN if local else _PROXIED).open(urllib.request.Request(url, json.dumps(body).encode(), h), timeout=timeout)
    raw = r.read().decode("utf-8", "replace")
    j = None
    for line in raw.splitlines():          # SSE：取最后一个带 id 的 data: 行；也兼容纯 JSON
        if line.startswith("data: "):
            try:
                d = json.loads(line[6:])
            except ValueError:
                continue
            if isinstance(d, dict) and "id" in d:
                j = d
    if j is None:
        j = json.loads(raw) if raw.strip() else {}
    return j, r.headers.get("Mcp-Session-Id")


def _init_body():
    return {"jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "crew", "version": "0.1"}}}


def _http_rpc(name, srv, body, timeout, env):
    def handshake():
        reply, sid = _post(srv, _init_body(), timeout, env)
        if reply.get("error"):
            raise RuntimeError(str(reply["error"]))
        _INSTRUCTIONS[name] = (reply.get("result") or {}).get("instructions") or ""
        _post(srv, {"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout, env, sid)
        _SESSION[name] = sid or ""
    if name not in _SESSION:
        handshake()
    try:
        return _post(srv, body, timeout, env, _SESSION.get(name) or None)[0]
    except urllib.error.HTTPError as e:
        if e.code not in (400, 404):
            raise
        handshake()                       # 会话过期或服务要求会话：握手后重发一次
        return _post(srv, body, timeout, env, _SESSION.get(name) or None)[0]


# ───────── stdio
class _Stdio:
    def __init__(self, name, srv, env):
        e = dict(env)
        e.update({k: str(v) for k, v in (srv.get("env") or {}).items()})
        self.p = subprocess.Popen([os.path.expanduser(srv["command"])] + [str(a) for a in srv.get("args") or []],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  env=e, cwd=os.path.expanduser(srv["cwd"]) if srv.get("cwd") else None, text=True, bufsize=1)
        self.lock = threading.Lock()
        self.n = 0
        reply = self.rpc(_init_body(), 60)
        if reply.get("error"):
            self.p.kill()
            raise RuntimeError(str(reply["error"]))
        _INSTRUCTIONS[name] = (reply.get("result") or {}).get("instructions") or ""
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def alive(self):
        return self.p.poll() is None

    def send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def rpc(self, body, timeout):
        with self.lock:
            self.n += 1
            body = dict(body, id=self.n)
            self.send(body)
            box = {}

            def read():
                for line in self.p.stdout:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(d, dict) and d.get("id") == body["id"] and ("result" in d or "error" in d):
                        box["v"] = d
                        return
            t = threading.Thread(target=read, daemon=True)
            t.start()
            t.join(timeout)
            if "v" not in box:
                self.p.kill()             # 超时或进程退出：杀掉，下次调用重启
                raise TimeoutError(f"stdio 服务 {timeout:.0f} 秒没有应答" if self.alive() else "stdio 服务已退出")
            return box["v"]


def _stdio_rpc(name, srv, body, timeout, env):
    with _LOCK:
        s = _STDIO.get(name)
        if not s or not s.alive():
            s = _STDIO[name] = _Stdio(name, srv, env)
    return s.rpc(body, timeout)


# ───────── 对外
def _rpc(server, method, params, timeout, cfg=None):
    """返回 (JSON-RPC 应答, 错误)。不抛异常。"""
    srv = dict(servers(cfg).get(server) or {})
    if not (srv.get("url") or srv.get("command")):
        return None, f"没有配置 MCP 服务 {server}"
    timeout = float(srv.get("timeout_s") or timeout)
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    env = secrets_env(cfg)
    where = srv.get("url") or srv.get("command")
    try:
        if srv.get("url"):
            return _http_rpc(server, srv, body, timeout, env), None
        return _stdio_rpc(server, srv, body, timeout, env), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP Error {e.code} from {where}: {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e} ({where})"


def call(server, tool, args=None, timeout=120, cfg=None):
    """返回 (结果, 错误)。文本块拼起来能解析成 JSON 就给 JSON，否则给原文。"""
    j, err = _rpc(server, "tools/call", {"name": tool, "arguments": args or {}}, timeout, cfg)
    if err:
        return None, err
    if not isinstance(j, dict) or j.get("error"):
        return None, (json.dumps(j.get("error"), ensure_ascii=False)[:300]
                      if isinstance(j, dict) else "没有返回 JSON-RPC 对象")
    res = j.get("result") or {}
    text = "\n".join(b.get("text", "") for b in res.get("content") or [] if b.get("type") == "text")
    if res.get("isError"):
        return None, text[:500] or "工具返回错误"
    try:
        return json.loads(text), None
    except ValueError:
        return text, None


@atexit.register
def close():
    for server in list(_STDIO.values()):
        if server.alive():
            server.p.terminate()
            try:
                server.p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                server.p.kill()
                server.p.wait()
        if server.p.stdin:
            server.p.stdin.close()
        if server.p.stdout:
            server.p.stdout.close()
    _STDIO.clear()
