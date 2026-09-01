#!/usr/bin/env bash
# =============================================================================
# logs.sh  —  从 Mac 远程查看 Windows 上的服务日志
#
# 用法：
#   ./logs.sh           # 实时追踪（tail -f 效果）
#   ./logs.sh -n 100    # 查看最后 100 行
#   ./logs.sh --clear   # 清空日志文件
# =============================================================================

WIN_HOST="172.30.90.102"
WIN_USER="kinch.zhu"
WIN_PORT=22
LOG_PATH="D:/deploy/cursor-account-manager/data/logs/cam.log"

SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10 -p "$WIN_PORT")

LINES=80
FOLLOW=true
CLEAR=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n) LINES="$2"; FOLLOW=false; shift 2 ;;
    -f) FOLLOW=true; shift ;;
    --clear) CLEAR=true; shift ;;
    *) echo "用法: $0 [-n 行数] [-f] [--clear]"; exit 0 ;;
  esac
done

echo "=== ${WIN_HOST} :: ${LOG_PATH} ==="

if $CLEAR; then
  ssh "${SSH_OPTS[@]}" "${WIN_USER}@${WIN_HOST}" \
    "powershell -Command \"Clear-Content '${LOG_PATH}' -ErrorAction SilentlyContinue; Write-Host 'cleared'\""
  echo "日志已清空"
  exit 0
fi

if ! $FOLLOW; then
  # 只看最后 N 行，读一次就退出
  ssh "${SSH_OPTS[@]}" "${WIN_USER}@${WIN_HOST}" \
    "powershell -Command \"
      [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
      if (Test-Path '${LOG_PATH}') {
        Get-Content '${LOG_PATH}' -Tail ${LINES} -Encoding UTF8
      } else {
        Write-Host '[日志文件不存在，服务可能尚未启动或未用 start-web.bat 启动]'
      }
    \""
  exit 0
fi

# 实时追踪：本地轮询，每次 SSH 读最新内容，Ctrl+C 停止
echo "(实时追踪，Ctrl+C 退出)"
echo ""

# 先读末尾若干行
LAST_LINES=$(ssh "${SSH_OPTS[@]}" "${WIN_USER}@${WIN_HOST}" \
  "powershell -Command \"
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    if (Test-Path '${LOG_PATH}') {
      Get-Content '${LOG_PATH}' -Tail ${LINES} -Encoding UTF8
    } else {
      Write-Host '[日志文件不存在，服务可能尚未启动]'
    }
  \"" 2>/dev/null)

echo "$LAST_LINES"
SEEN_LINES=$(echo "$LAST_LINES" | wc -l | tr -d ' ')

# 持续轮询新内容
while true; do
  sleep 2
  NEW=$(ssh "${SSH_OPTS[@]}" "${WIN_USER}@${WIN_HOST}" \
    "powershell -Command \"
      [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
      if (Test-Path '${LOG_PATH}') {
        \\\$all = Get-Content '${LOG_PATH}' -Encoding UTF8
        if (\\\$all.Count -gt ${SEEN_LINES}) {
          \\\$all[${SEEN_LINES}..\\\$all.Count] -join \\\"\`n\\\"
        }
      }
    \"" 2>/dev/null)

  if [[ -n "$NEW" ]]; then
    echo "$NEW"
    NEW_COUNT=$(echo "$NEW" | wc -l | tr -d ' ')
    SEEN_LINES=$((SEEN_LINES + NEW_COUNT))
  fi
done
