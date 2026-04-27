#!/usr/bin/env bash
# =============================================================================
# deploy-to-windows.sh
# 从 Mac 本地将 cursor-account-manager 远程部署到干净的 Windows 机器
#
# 前提：目标 Windows 已开启 OpenSSH Server（Win10/11 可选功能 → OpenSSH 服务器）
# 用法：
#   chmod +x deploy-to-windows.sh
#   ./deploy-to-windows.sh -h <WIN_HOST> -u <WIN_USER> [-p <PORT>] [-k <SSH_KEY>]
#
# 示例：
#   ./deploy-to-windows.sh -h 192.168.1.100 -u Administrator
#   ./deploy-to-windows.sh -h 192.168.1.100 -u Administrator -k ~/.ssh/id_rsa
# =============================================================================

set -euo pipefail

# ---------- 颜色输出 ----------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ---------- 参数解析 ----------
WIN_HOST="172.30.90.102"
WIN_USER="kinch.zhu"
WIN_PORT=22
SSH_KEY=""
REMOTE_DIR="C:/deploy/cursor-account-manager"
SKIP_CHROME=true   # Chrome 已预装，默认跳过

usage() {
  cat <<EOF
用法: $0 -h <主机> -u <用户名> [选项]

必填:
  -h <host>    Windows 目标机器 IP 或域名
  -u <user>    SSH 登录用户名（通常是 Administrator 或普通用户名）

选项:
  -p <port>    SSH 端口（默认 22）
  -k <key>     SSH 私钥路径（不填则使用密码登录）
  -d <dir>     远程部署目录（默认 C:/deploy/cursor-account-manager）
  --no-chrome  跳过 Chrome 安装（已安装时使用）
  --no-start   部署完成后不自动启动 Web 服务
  --help       显示此帮助

EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h) WIN_HOST="$2"; shift 2 ;;
    -u) WIN_USER="$2"; shift 2 ;;
    -p) WIN_PORT="$2"; shift 2 ;;
    -k) SSH_KEY="$2"; shift 2 ;;
    -d) REMOTE_DIR="$2"; shift 2 ;;
    --no-chrome) SKIP_CHROME=true; shift ;;
    --help) usage ;;
    *) error "未知参数: $1，使用 --help 查看帮助" ;;
  esac
done

# ---------- SSH ControlMaster：所有连接复用，只输一次密码 ----------
CTRL_SOCK="/tmp/ssh-cam-$(date +%s).sock"

SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=15
          -o ControlMaster=auto
          -o ControlPath="$CTRL_SOCK"
          -o ControlPersist=60
          -p "$WIN_PORT")
SCP_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=15
          -o ControlMaster=auto
          -o ControlPath="$CTRL_SOCK"
          -o ControlPersist=60
          -P "$WIN_PORT")
[[ -n "$SSH_KEY" ]] && SSH_OPTS+=(-i "$SSH_KEY") && SCP_OPTS+=(-i "$SSH_KEY")

# 脚本退出时关闭 master 连接
trap 'ssh -O exit -o ControlPath="$CTRL_SOCK" "${WIN_USER}@${WIN_HOST}" 2>/dev/null; rm -f "$CTRL_SOCK"' EXIT

ssh_run() {
  ssh "${SSH_OPTS[@]}" "${WIN_USER}@${WIN_HOST}" "powershell -NoProfile -NonInteractive -Command $1"
}

scp_upload() {
  local src="$1" dst="$2"
  scp "${SCP_OPTS[@]}" "$src" "${WIN_USER}@${WIN_HOST}:${dst}"
}

# ---------- 检查本地依赖 ----------
info "检查本地工具..."
command -v ssh  >/dev/null || error "本地未找到 ssh，请安装 OpenSSH client"
command -v scp  >/dev/null || error "本地未找到 scp"
command -v zip  >/dev/null || { command -v tar >/dev/null || error "本地未找到 zip/tar"; }

# ---------- 确认项目根目录 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"   # deploy-to-windows.sh 放在项目根下

info "项目目录: $PROJECT_DIR"
[[ -f "$PROJECT_DIR/requirements.txt" ]] || error "未找到 requirements.txt，请确认脚本放在 cursor-account-manager/ 目录下"

# ---------- 打包项目（排除无用文件） ----------
PACK_NAME="cam-$(date +%Y%m%d_%H%M%S).zip"
PACK_PATH="/tmp/$PACK_NAME"

info "打包项目 -> ${PACK_PATH} (排除 .venv / __pycache__ / .git)..."
cd "$PROJECT_DIR/.."
zip -r "$PACK_PATH" cursor-account-manager \
  --exclude "cursor-account-manager/.venv/*" \
  --exclude "cursor-account-manager/__pycache__/*" \
  --exclude "cursor-account-manager/cam/__pycache__/*" \
  --exclude "cursor-account-manager/.git/*" \
  --exclude "cursor-account-manager/data/exports/*" \
  --exclude "cursor-account-manager/data/browser_profiles/*" \
  --exclude "cursor-account-manager/*.db" \
  --exclude "*/.DS_Store" \
  --exclude "*/__pycache__/*" \
  --exclude "*.pyc" \
  > /dev/null
success "打包完成: $(du -sh "${PACK_PATH}" | cut -f1)"

# ---------- 测试 SSH 连通性 ----------
info "测试 SSH 连通性 → ${WIN_USER}@${WIN_HOST}:${WIN_PORT}..."
ssh_run "Write-Host 'SSH_OK'" 2>/dev/null | grep -q "SSH_OK" \
  || error "SSH 连接失败，请检查：\n  1. Windows 是否已运行 windows-init.ps1 开启了 SSH\n  2. 防火墙是否放行端口 $WIN_PORT\n  3. 用户名/密码/密钥是否正确\n  提示：在 Windows 上运行 → powershell -ExecutionPolicy Bypass -File windows-init.ps1"
success "SSH 连接正常"

# ---------- 上传包到 Windows 临时目录 ----------
REMOTE_TMP="C:/Windows/Temp/$PACK_NAME"
info "上传代码包 → $WIN_USER@$WIN_HOST:$REMOTE_TMP ..."
scp_upload "$PACK_PATH" "$REMOTE_TMP"
success "上传完成"

# ---------- 上传 Windows 安装脚本（加 UTF-8 BOM，让 Windows PowerShell 正确识别中文）----------
SETUP_PS1="$PROJECT_DIR/windows-setup.ps1"
[[ -f "$SETUP_PS1" ]] || error "未找到 windows-setup.ps1，请确认两个脚本在同一目录"
SETUP_PS1_BOM="/tmp/windows-setup-bom.ps1"
printf '\xef\xbb\xbf' > "$SETUP_PS1_BOM"   # UTF-8 BOM
cat "$SETUP_PS1" >> "$SETUP_PS1_BOM"
REMOTE_SETUP="C:/Windows/Temp/windows-setup.ps1"
info "上传安装脚本 → $REMOTE_SETUP ..."
scp_upload "$SETUP_PS1_BOM" "$REMOTE_SETUP"
rm -f "$SETUP_PS1_BOM"
success "安装脚本上传完成"

# ---------- 远程执行安装脚本 ----------
info "开始在远程 Windows 执行安装脚本（首次约需 5-15 分钟）..."
echo ""

SKIP_CHROME_FLAG=""
$SKIP_CHROME && SKIP_CHROME_FLAG="-SkipChrome"

ssh "${SSH_OPTS[@]}" "${WIN_USER}@${WIN_HOST}" \
  "chcp 65001 >nul 2>&1 & powershell -NoProfile -ExecutionPolicy Bypass \
   -File C:\\Windows\\Temp\\windows-setup.ps1 \
   -PackagePath \"${REMOTE_TMP}\" \
   -DeployDir \"${REMOTE_DIR}\" \
   ${SKIP_CHROME_FLAG} -StartServer"

echo ""
success "========================================"
success "  部署完成！"
success "========================================"

echo ""
success "========================================"
success "  Web UI  : http://${WIN_HOST}:8765"
success "  浏览器  : 有痕模式，需要 Windows 用户已登录桌面/RDP"
success "  Ctrl+C 退出日志追踪（服务继续运行）"
success "========================================"

# ---------- 清理本地临时包 ----------
rm -f "$PACK_PATH"

# ---------- 实时追踪日志，Ctrl+C 才退出 ----------
LOG_REMOTE="C:/deploy/cursor-account-manager/data/logs/cam.log"
info "实时日志（Ctrl+C 退出追踪，服务不受影响）..."
echo ""

SEEN=0
while true; do
  NEW=$(ssh "${SSH_OPTS[@]}" "${WIN_USER}@${WIN_HOST}" \
    "powershell -NoProfile -Command \"\
      [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; \
      if(Test-Path '${LOG_REMOTE}'){\
        \$all=Get-Content '${LOG_REMOTE}' -Encoding UTF8; \
        if(\$all.Count -gt ${SEEN}){\$all[${SEEN}..(\$all.Count-1)] -join [char]10}\
      }\
    \"" 2>/dev/null)
  if [[ -n "$NEW" ]]; then
    echo "$NEW"
    SEEN=$((SEEN + $(echo "$NEW" | wc -l | tr -d ' ')))
  fi
  sleep 2
done
