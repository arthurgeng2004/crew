#!/usr/bin/env bash
# crew 安装脚本：链接命令行、生成配置、写入 API key、让 Claude Code 加载插件。
# 用法：./install.sh            交互式（问 key）
#       ./install.sh --no-key   不问 key（之后自己写 ~/.crew/deepseek.env）
#       ./install.sh --no-claude 不改 ~/.claude/settings.json
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREW_HOME="${CREW_HOME:-$HOME/.crew}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
ASK_KEY=1
SET_CLAUDE=1
for a in "$@"; do
  case "$a" in
    --no-key) ASK_KEY=0 ;;
    --no-claude) SET_CLAUDE=0 ;;
    -h|--help) sed -n 2,6p "$0"; exit 0 ;;
    *) echo "不认识的参数：$a" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*" >&2; }

# 1. 依赖
command -v python3 >/dev/null || { echo "需要 python3（3.8+）" >&2; exit 1; }
python3 - <<'EOF' || { echo "需要 Python 3.8 或更高版本" >&2; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)
EOF
if command -v node >/dev/null; then
  NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
  [ "$NODE_MAJOR" -ge 18 ] || warn "node 版本 < 18：托管工作流（crew wf）可能跑不了"
else
  warn "没找到 node：任务能用，托管工作流（crew wf）需要 Node.js 18+"
fi
command -v codex >/dev/null || say "没找到 codex：gpt 档案直接用本机已登录的 Codex（可选，装好后 codex login 即可）"

# 2. 命令行
mkdir -p "$BIN_DIR"
chmod +x "$ROOT/bin/crew" "$ROOT"/workerbin/*
ln -sfn "$ROOT/bin/crew" "$BIN_DIR/crew"
say "已链接 $BIN_DIR/crew → $ROOT/bin/crew"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "$BIN_DIR 不在 PATH 里，把它加进 shell 配置：export PATH=\"$BIN_DIR:\$PATH\"" ;; esac

# 3. 配置（第一次运行自动生成 ~/.crew/config.json）
mkdir -p "$CREW_HOME"; chmod 700 "$CREW_HOME"
CREW_HOME="$CREW_HOME" python3 "$ROOT/bin/crew" ls >/dev/null 2>&1 || true
[ -f "$CREW_HOME/config.json" ] && say "配置：$CREW_HOME/config.json"

# 4. API key（只写进 ~/.crew/*.env，权限 600，不进仓库）
write_key() {  # 文件 变量名 提示
  local file="$1" var="$2" prompt="$3" key
  if [ -s "$file" ]; then say "$file 已存在，跳过"; return; fi
  read -r -s -p "$prompt（回车跳过）：" key; echo
  [ -z "$key" ] && return
  umask 077; printf '%s=%s\n' "$var" "$key" > "$file"; chmod 600 "$file"
  say "已写入 $file"
}
if [ "$ASK_KEY" = 1 ] && [ -t 0 ]; then
  write_key "$CREW_HOME/deepseek.env" DEEPSEEK_API_KEY "DeepSeek API key（默认工人 ds 用）"
  write_key "$CREW_HOME/openai.env" OPENAI_API_KEY "OpenAI API key（可选，只有 gpt-chat 档案用；gpt 档案用已登录的 Codex）"
fi

# 5. 让 Claude Code 加载插件：在 ~/.claude/settings.json 的 env 里加 CLAUDE_CODE_PLUGIN_DIRS
if [ "$SET_CLAUDE" = 1 ]; then
  SETTINGS="$HOME/.claude/settings.json"
  mkdir -p "$HOME/.claude"
  python3 - "$SETTINGS" "$ROOT/plugin" <<'EOF'
import json, os, sys
path, plug = sys.argv[1], sys.argv[2]
d = {}
if os.path.exists(path):
    try:
        d = json.load(open(path, encoding="utf-8"))
    except ValueError:
        sys.exit(f"{path} 不是合法 JSON，没有改动；请手动在 env 里加 CLAUDE_CODE_PLUGIN_DIRS={plug}")
env = d.setdefault("env", {})
dirs = [x for x in (env.get("CLAUDE_CODE_PLUGIN_DIRS") or "").split(":") if x]
if plug not in dirs:
    dirs.append(plug)
    env["CLAUDE_CODE_PLUGIN_DIRS"] = ":".join(dirs)
    note = ""
    if os.path.exists(path):
        os.replace(path, path + ".bak-crew")
        note = "（原文件备份为 settings.json.bak-crew）"
    json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"==> 已在 {path} 的 env 里加上插件目录{note}")
else:
    print(f"==> {path} 已包含插件目录")
EOF
fi

# 6. 启动后台服务
CREW_HOME="$CREW_HOME" python3 "$ROOT/bin/crew" up || warn "后台服务没起来，稍后运行 crew up"

cat <<EOF

安装完成。接下来：
  crew models ds         检查 DeepSeek 接口和 key
  crew run -p mock "测试"  不调模型走一遍流程
  新开一个 Claude Code 会话，输入 /crew 打开面板
配置文件：$CREW_HOME/config.json（档案、MCP 服务、并发等，见 README）
EOF
