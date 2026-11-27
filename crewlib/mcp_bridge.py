"""crew 自带的 MCP 桥：给工人（没有原生 MCP 客户端的模型）调 streamable-HTTP 的 MCP 服务。

服务从 ~/.crew/config.json 读：
  "mcp_servers": {"名字": {"url": "http://127.0.0.1:8001/mcp", "purpose": "一句话用途", "headers": {"Authorization": "Bearer …"}}}
  "mcp_from_claude_json": true   另外读 ~/.claude.json 里 url 是 http(s) 的 mcpServers（含 headers）
环境变量 MCP_URL_<名字大写> 可以覆盖某个服务的地址。
headers 的值可以写 ${ENV_NAME}，运行时从环境变量替换，密钥不用写进配置文件。
"""
import json
import urllib.error
import os
import re
import urllib.request

from . import core

_SESSION = {}
_PLAIN = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机 / 内网服务不走代理
_PROXIED = urllib.request.build_opener()


def _expand(v):
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), str(v))


def servers():
    """{名字: {"url", "headers", "purpose"}}"""
    cfg = core.config()
    out = {}
    if cfg.get("mcp_from_claude_json"):
        try:
            cj = json.load(open(os.path.expanduser("~/.claude.json"), encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cj = {}

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == "mcpServers" and isinstance(v, dict):
                        for name, c in v.items():
                            if isinstance(c, dict) and str(c.get("url") or "").startswith("http"):
                                out.setdefault(name, {"url": c["url"], "headers": c.get("headers") or {}})
                    walk(v)
        walk(cj)
    for name, c in (cfg.get("mcp_servers") or {}).items():
        c = {"url": c} if isinstance(c, str) else dict(c or {})
        if c.get("url"):
            out[name] = c
    for name, c in out.items():
        env = os.environ.get("MCP_URL_" + name.upper().replace("-", "_"))
        if env:
            c["url"] = env
    return out


def server_urls():
    return {k: v["url"] for k, v in servers().items()}


def purpose(name):
    return str((servers().get(name) or {}).get("purpose") or "")


def _post(srv, body, timeout, sid=None):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    h.update({k: _expand(v) for k, v in (srv.get("headers") or {}).items()})
    if sid:
        h["Mcp-Session-Id"] = sid
    url = srv["url"]
    local = re.match(r"https?://(127\.|localhost|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", url)
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


def _handshake(name, srv, timeout):
    init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "crew", "version": "0.1"}}}
    _, sid = _post(srv, init, timeout)
    if sid:
        try:
            _post(srv, {"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout, sid)
        except Exception:  # noqa: BLE001
            pass
    _SESSION[name] = sid or ""
    return sid


def _rpc(server, method, params, timeout, url=None):
    """返回 (JSON-RPC 应答, 错误)。先用已有会话直接调；服务要求会话（400 / 404）就握手后重发。"""
    srv = dict(servers().get(server) or {})
    if url:
        srv["url"] = url
    if not srv.get("url"):
        return None, f"没有配置 MCP 服务 {server}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        if server not in _SESSION:
            _handshake(server, srv, timeout)
        try:
            return _post(srv, body, timeout, _SESSION.get(server) or None)[0], None
        except urllib.error.HTTPError as e:
            if e.code not in (400, 404):
                raise
            _handshake(server, srv, timeout)
            return _post(srv, body, timeout, _SESSION.get(server) or None)[0], None
    except urllib.error.HTTPError as e:
        return None, f"HTTP Error {e.code} from {srv['url']}: {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e} ({srv['url']})"


def call(server, tool, args=None, timeout=120):
    """返回 (结果, 错误)。结果是文本块拼起来后能解析成 JSON 就给 JSON，否则给原文。"""
    j, err = _rpc(server, "tools/call", {"name": tool, "arguments": args or {}}, timeout)
    if err:
        return None, err
    if not isinstance(j, dict) or j.get("error"):
        return None, json.dumps((j or {}).get("error"), ensure_ascii=False)[:300]
    res = j.get("result") or {}
    text = "\n".join(b.get("text", "") for b in res.get("content") or [] if b.get("type") == "text")
    if res.get("isError"):
        return None, text[:500] or "工具返回错误"
    try:
        return json.loads(text), None
    except ValueError:
        return text, None
