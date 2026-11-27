#!/bin/bash
# macOS Bash 3.2 and Linux Bash. No shell startup files are edited.
set -eu
ROOT=$(CDPATH='' cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CREW_HOME=${CREW_HOME:-$HOME/.crew}
BIN_DIR=${BIN_DIR:-$HOME/.local/bin}
SET_CLAUDE=1
START=1
for arg in "$@"; do
  case "$arg" in
    --no-claude) SET_CLAUDE=0 ;;
    --no-start) START=0 ;;
    -h|--help)
      printf '%s\n' '用法：./install.sh [--no-claude] [--no-start]' \
        '环境变量：CREW_HOME、BIN_DIR、CREW_PYTHON（解释器路径）' \
        '--no-claude：不改 Claude 设置；--no-start：暂不启动服务。'
      exit 0 ;;
    *) printf '不认识的参数：%s\n' "$arg" >&2; exit 2 ;;
  esac
done
warn() { printf '[提示] %s\n' "$*" >&2; }
PYTHON=${CREW_PYTHON:-$(command -v python3 || true)}
[ -n "$PYTHON" ] || { warn '需要 Python 3.9+，请先安装 python3。'; exit 1; }
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "需要 Python 3.9+")'
PYTHON=$("$PYTHON" -c 'import sys; print(sys.executable)')
export CREW_HOME CREW_PYTHON="$PYTHON"
if command -v node >/dev/null 2>&1; then
  NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
  if [ "$NODE_MAJOR" -lt 18 ]; then warn '工作流需要 Node.js 18+；普通任务仍可用。'; fi
else
  warn '没有 node：普通任务仍可用，工作流需要 Node.js 18+。'
fi
if command -v codex >/dev/null 2>&1; then
  "$PYTHON" - <<'PY'
import subprocess
try:
    result = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=15)
    if result.returncode:
        print("[提示] Codex 尚未登录或无法确认登录，请先运行 codex login。")
    else:
        print("Codex 登录状态：已登录")
except (OSError, subprocess.TimeoutExpired):
    print("[提示] 无法确认 Codex 登录状态，请手动运行 codex login status。")
PY
else
  warn '没有 codex：默认工人需要安装 Codex CLI 并运行 codex login；mock 自检可先运行。'
fi
if ! command -v claude >/dev/null 2>&1; then
  warn '没有 claude：请安装 Claude Code CLI 并登录后使用 /crew；命令行自检仍可用。'
fi
"$PYTHON" "$ROOT/tools/install_config.py" install "$ROOT" "$BIN_DIR" "$SET_CLAUDE"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR 不在 PATH；可使用 $BIN_DIR/crew，或将此目录加入 PATH。" ;;
esac
if [ "$START" = 1 ]; then "$ROOT/bin/crew" up; fi
printf '%s\n' '安装完成。crew run -p mock "测试" 自检；新开 Claude Code 会话，输入 /crew。'
