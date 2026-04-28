#!/usr/bin/env bash
# =============================================================================
# restart.sh — 重启本地 cam web（先 kill 再启动）
#
# 用法：
#   ./restart.sh
#   ./restart.sh --tail 80
# =============================================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
LOG_DIR="$ROOT_DIR/data/logs"
LOG_FILE="$LOG_DIR/cam.log"
PID_FILE="$ROOT_DIR/data/cam-web.pid"
HOST="0.0.0.0"
PORT="8765"
TAIL_LINES=40

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tail)
      TAIL_LINES="${2:-40}"
      shift 2
      ;;
    *)
      echo "用法: $0 [--tail N]"
      exit 1
      ;;
  esac
done

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[ERROR] 未找到虚拟环境 Python: $PYTHON_BIN"
  echo "请先执行: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

mkdir -p "$LOG_DIR"

echo "[INFO] 停止旧进程..."
if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
  fi
fi

# 兜底：按端口清理
if command -v lsof >/dev/null 2>&1; then
  PIDS="$(lsof -ti tcp:"$PORT" || true)"
  if [[ -n "$PIDS" ]]; then
    echo "$PIDS" | xargs kill 2>/dev/null || true
    sleep 1
  fi
fi

# 再兜底：按命令行清理
if command -v pgrep >/dev/null 2>&1; then
  PIDS="$(pgrep -f "python.*-m cam web" || true)"
  if [[ -n "$PIDS" ]]; then
    echo "$PIDS" | xargs kill 2>/dev/null || true
    sleep 1
  fi
fi

echo "[INFO] 启动新进程..."
cd "$ROOT_DIR"
nohup "$PYTHON_BIN" -X utf8 -m cam web --host "$HOST" --port "$PORT" > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

sleep 1
if kill -0 "$NEW_PID" 2>/dev/null; then
  echo "[OK] 重启成功，PID=$NEW_PID"
  echo "[OK] Web UI: http://127.0.0.1:$PORT"
  echo "[OK] 日志: $LOG_FILE"
  echo "----------------------------------------"
  if [[ -f "$LOG_FILE" ]]; then
    tail -n "$TAIL_LINES" "$LOG_FILE" || true
  fi
else
  echo "[ERROR] 启动失败，请检查日志: $LOG_FILE"
  exit 1
fi
