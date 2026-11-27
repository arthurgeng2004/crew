#!/bin/bash
set -eu
ROOT=$(CDPATH='' cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CREW_HOME=${CREW_HOME:-$HOME/.crew}
BIN_DIR=${BIN_DIR:-$HOME/.local/bin}
PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    -h|--help) printf '%s\n' '用法：./uninstall.sh [--purge]（默认保留任务、配置和备份；--purge 删除 Crew 状态目录）'; exit 0 ;;
    *) printf '不认识的参数：%s\n' "$arg" >&2; exit 2 ;;
  esac
done
export CREW_HOME
"$ROOT/bin/crew" down
"$ROOT/workerbin/python3" "$ROOT/tools/install_config.py" uninstall "$ROOT" "$BIN_DIR" "$PURGE"
printf '%s\n' '卸载完成。可自行删除安装目录；Claude 设置备份仍保留在原处。'
