"""Claude 工人用的 MCP 服务（stdio）：把 crew 的工具（submit_result、ask_lead、report_problem、web_fetch）挂进 Claude Code。

由 clauderun 生成的 --mcp-config 拉起：python -m crewlib.claudemcp <任务目录>
工具清单读任务目录下的 claude-tools.json（clauderun 写）；每次调用连 claude-sock 里的 Unix socket 路径（目录过长时放入私有临时目录），
由跑这个任务的 crew 工人线程处理（和 Codex 的动态工具同一套 dyn_call），把结果原样返回。
"""
import json
import os
import socket
import sys


def call(sock_path, name, args):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(None)
    try:
        s.connect(sock_path)
        s.sendall((json.dumps({"name": name, "args": args}, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        r = json.loads(buf.decode("utf-8") or "{}")
        return r.get("text", ""), bool(r.get("ok"))
    except (OSError, ValueError) as e:
        return f"错误：连不上 crew（{e}）。crew 后台可能在重启，稍后再调用一次。", False
    finally:
        s.close()


def main():
    tdir = sys.argv[1]
    try:
        addr = open(os.path.join(tdir, "claude-sock"), encoding="utf-8").read().strip()
    except OSError:
        addr = os.path.join(tdir, "tools.sock")
    sock_path = addr
    tools = json.load(open(os.path.join(tdir, "claude-tools.json"), encoding="utf-8"))
    out = sys.stdout

    def send(o):
        out.write(json.dumps(o, ensure_ascii=False) + "\n")
        out.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except ValueError:
            continue
        i, meth = m.get("id"), m.get("method")
        if i is None:
            continue                      # 通知（initialized 等）不用回
        if meth == "initialize":
            pv = (m.get("params") or {}).get("protocolVersion") or "2025-06-18"
            send({"jsonrpc": "2.0", "id": i, "result": {"protocolVersion": pv, "capabilities": {"tools": {}},
                                                       "serverInfo": {"name": "crewtask", "version": "1"}}})
        elif meth == "tools/list":
            send({"jsonrpc": "2.0", "id": i, "result": {"tools": tools}})
        elif meth == "tools/call":
            p = m.get("params") or {}
            a = p.get("arguments") or {}
            text, ok = call(sock_path, p.get("name"), a if isinstance(a, dict) else {})
            send({"jsonrpc": "2.0", "id": i, "result": {"content": [{"type": "text", "text": text}], "isError": not ok}})
        elif meth == "ping":
            send({"jsonrpc": "2.0", "id": i, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": f"不支持 {meth}"}})


if __name__ == "__main__":
    main()
