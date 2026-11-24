#!/usr/bin/env bash
# =============================================================================
# devin-ops.sh —— Devin 平台运维脚本（Linux / macOS / CentOS）
#
# 用法： scripts/devin-ops.sh <命令> [参数]
#   import <文件>          批量导入账号（CSV/Excel）；加 --overwrite 覆盖已有
#   status [--failed]      查看账号与最近一次采集结果
#   collect [--email X]…   立即采集（手动口径，默认不发飞书；加 --notify 发送）
#   daily                  按“每日凌晨任务”口径采集并发飞书（给系统 cron 用）
#   retry-failed           只重采最近一次失败的账号
#   probe <邮箱> [--headed] 单账号探针，打印解析结果；失败会落截图/页面文本
#   notify-test [--send]   生成播报卡片自检（默认只打印）
#   cycles [--email X]     查看已入库的账期
#   invoices [--email X] [--out 目录]  实时读取发票并下载 PDF，打包成 zip 存到目录（默认当前目录，不入库）
#   next-run               下一次定时采集时间
#   artifacts [N]          列出最近 N 个失败现场文件（默认 10）
#   logs [N]               查看服务日志最后 N 行（默认 100）
#   backup-db              备份 tokens.db（含 Devin 三张表）
#   cron-line              打印可直接粘贴的 crontab 行（不用 Web 内置调度时）
#
# 说明：Web 服务（python -m cam web）已内置每日调度，通常不需要 cron；
#       仅当 Web 服务不常驻时才用 `daily` + cron，并设置 DEVIN_SCHEDULE_ENABLE=false 避免重复。
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# 从 .env 安全读取单个键（不 source，避免密码里的 # $ 空格被 shell 解释）
env_get() {
  local key="$1" def="${2:-}" v=""
  [[ -f .env ]] && v="$(grep -E "^${key}=" .env | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' || true)"
  echo "${v:-${!key:-$def}}"
}

if   [[ -x .venv/bin/python ]];         then PY=.venv/bin/python
elif [[ -x .venv/Scripts/python.exe ]]; then PY=.venv/Scripts/python.exe
else PY="$(command -v python3 || command -v python)"; fi

cam() { PYTHONUTF8=1 "$PY" -m cam "$@"; }

cmd="${1:-help}"; shift || true

case "$cmd" in
  import)       [[ $# -ge 1 ]] || { echo "用法: import <文件> [--overwrite]"; exit 2; }
                cam devin-import "$@" ;;
  status)       cam devin-status "$@" ;;
  collect)      cam devin-collect "$@" ;;
  daily)        cam devin-collect --trigger scheduler "$@" ;;
  retry-failed)
    FAILED=(); while IFS= read -r line; do [[ -n "$line" ]] && FAILED+=("$line"); done < <(cam devin-status --failed | awk 'NR>1 && $1 ~ /@/ {print $1}')
    if [[ ${#FAILED[@]} -eq 0 ]]; then echo "没有失败账号"; exit 0; fi
    ARGS=(); for e in "${FAILED[@]}"; do ARGS+=(--email "$e"); done
    echo "重采 ${#FAILED[@]} 个失败账号…"
    cam devin-collect "${ARGS[@]}" --notify ;;
  probe)        [[ $# -ge 1 ]] || { echo "用法: probe <邮箱> [--headed] [--save]"; exit 2; }
                EMAIL="$1"; shift; cam devin-probe "$EMAIL" "$@" ;;
  notify-test)  cam devin-notify-test "$@" ;;
  cycles)       cam devin-cycles "$@" ;;
  invoices)     cam devin-invoices "$@" ;;
  next-run)     cam devin-next-run ;;
  artifacts)
    DIR="$(env_get DEVIN_ARTIFACTS_DIR data/artifacts/devin)"
    ls -lt "$DIR" 2>/dev/null | head -n "$(( ${1:-10} + 1 ))" || echo "目录不存在: $DIR" ;;
  logs)
    LOG="data/logs/cam.log"; [[ -f "$LOG" ]] || LOG="$(ls -t data/logs/*.log 2>/dev/null | head -1 || true)"
    [[ -n "${LOG:-}" ]] && tail -n "${1:-100}" "$LOG" | grep -i --line-buffered -E 'devin|ERROR' || echo "未找到日志" ;;
  backup-db)
    DB="$(env_get TOKENS_DB data/tokens.db)"; OUT="${DB%.db}.$(date +%Y%m%d-%H%M%S).bak"
    "$PY" - "$DB" "$OUT" <<'PYEOF'
import sqlite3, sys
src, dst = sys.argv[1:3]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
print("已备份 ->", dst)
PYEOF
    ;;
  cron-line)
    echo "# 每日 02:30 采集 Devin 并发飞书（需 DEVIN_SCHEDULE_ENABLE=false，避免与 Web 内置调度重复）"
    echo "30 2 * * * cd $ROOT && scripts/devin-ops.sh daily >> data/logs/devin-daily.log 2>&1" ;;
  help|-h|--help|*) sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
esac
