"""Small stdio MCP fixture. Used only by the public interface tests."""
import base64
import json
import sys


def reply(request):
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fixture", "version": "1"}, "instructions": "Fixture instructions"}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo a message", "inputSchema": {
            "type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}}]}
    elif method == "tools/call":
        params = request.get("params") or {}
        if params.get("name") == "image":
            result = {"content": [{"type": "image", "mimeType": "image/png",
                                   "data": base64.b64encode(b"fixture-image").decode()}]}
        elif params.get("name") == "error":
            result = {"content": [{"type": "text", "text": "Fixture tool error"}], "isError": True}
        else:
            result = {"content": [{"type": "text", "text": json.dumps(params.get("arguments") or {})}]}
    else:
        return None
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


if __name__ == "__main__":
    for line in sys.stdin:
        value = reply(json.loads(line))
        if value is not None:
            print(json.dumps(value), flush=True)
