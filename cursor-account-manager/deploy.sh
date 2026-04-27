#!/usr/bin/env bash
# ============================================================
# deploy.sh — 一键部署 Cursor Account Manager 到 CentOS 服务器
# 用法: bash deploy.sh
# ============================================================
set -euo pipefail

# ── 目标服务器配置 ─────────────────────────────────────────────
REMOTE_HOST="172.30.11.122"
REMOTE_USER="centos"
REMOTE_PASS="centos11.122"
REMOTE_PORT=22
REMOTE_DIR="/opt/cursor-account-manager"
APP_PORT=8765
SERVICE_NAME="cam-web"

# ── 本地项目根目录（脚本所在目录）──────────────────────────────
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 颜色 ───────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $*"; }
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; exit 1; }
step() { echo; echo -e "${BOLD}══ $* ══${NC}"; }

# ── SSH / SCP / RSYNC 封装（带自动重试）────────────────────────
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15 -p ${REMOTE_PORT}"
SCP_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15 -P ${REMOTE_PORT}"

# 带重试的 ssh（最多 3 次，间隔 5s）
ssh_run() {
    local attempt=1
    while [ $attempt -le 3 ]; do
        sshpass -p "${REMOTE_PASS}" ssh ${SSH_OPTS} "${REMOTE_USER}@${REMOTE_HOST}" "$@" && return 0
        warn "SSH 第 ${attempt}/3 次失败，5s 后重试..."
        sleep 5
        attempt=$((attempt + 1))
    done
    return 1
}

# sudo 版 ssh（带重试）
_sudoers_file="/etc/sudoers.d/cam_deploy"
_setup_nopasswd() {
    local attempt=1
    while [ $attempt -le 3 ]; do
        sshpass -p "${REMOTE_PASS}" ssh ${SSH_OPTS} "${REMOTE_USER}@${REMOTE_HOST}" \
            "echo '${REMOTE_PASS}' | sudo -S -p '' bash -c \
            \"echo '${REMOTE_USER} ALL=(ALL) NOPASSWD:ALL' > ${_sudoers_file} && chmod 440 ${_sudoers_file}\"" \
            && return 0
        warn "NOPASSWD 配置第 ${attempt}/3 次失败，重试..."
        sleep 5
        attempt=$((attempt + 1))
    done
    return 1
}
_cleanup_nopasswd() {
    sshpass -p "${REMOTE_PASS}" ssh ${SSH_OPTS} "${REMOTE_USER}@${REMOTE_HOST}" \
        "sudo rm -f ${_sudoers_file}" 2>/dev/null || true
}
ssh_sudo() {
    local attempt=1
    while [ $attempt -le 3 ]; do
        sshpass -p "${REMOTE_PASS}" ssh ${SSH_OPTS} "${REMOTE_USER}@${REMOTE_HOST}" "sudo $*" && return 0
        warn "sudo 命令第 ${attempt}/3 次失败，重试..."
        sleep 5
        attempt=$((attempt + 1))
    done
    return 1
}

# 文件上传（scp 用大写 -P 指定端口，带重试）
scp_put() {
    local attempt=1
    while [ $attempt -le 3 ]; do
        sshpass -p "${REMOTE_PASS}" scp ${SCP_OPTS} -r "$1" "${REMOTE_USER}@${REMOTE_HOST}:$2" && return 0
        warn "scp 第 ${attempt}/3 次失败，重试..."
        sleep 5
        attempt=$((attempt + 1))
    done
    return 1
}

# rsync 同步（排除不需要的文件）
rsync_push() {
    sshpass -p "${REMOTE_PASS}" rsync -az --delete \
        --exclude='.venv/' \
        --exclude='.conda/' \
        --exclude='__pycache__/' \
        --exclude='*.pyc' \
        --exclude='.env' \
        --exclude='data/tokens.db' \
        --exclude='data/exports/' \
        --exclude='data/accounts.csv' \
        --exclude='.git/' \
        --exclude='*.egg-info/' \
        -e "ssh ${SSH_OPTS}" \
        "$1" "${REMOTE_USER}@${REMOTE_HOST}:$2"
}

# ══════════════════════════════════════════════════════════════
# Step 1: 检查本地工具 & 连通性
# ══════════════════════════════════════════════════════════════
step "Step 1  检查环境 & 连通性"

command -v sshpass >/dev/null 2>&1 || err "sshpass 未安装。请先执行: brew install sshpass"
command -v rsync   >/dev/null 2>&1 || err "rsync 未安装"
log "本地工具 OK"

log "测试 SSH 连接 ${REMOTE_USER}@${REMOTE_HOST} ..."
ssh_run "echo 'SSH OK'" || err "SSH 连接失败（3 次均失败），请检查 IP / 账号 / 密码 / 网络"
ok "SSH 连接成功"

OS_VER=$(ssh_run "cat /etc/centos-release 2>/dev/null || cat /etc/redhat-release 2>/dev/null || echo unknown")
log "远程系统: ${OS_VER}"

log "配置临时 sudo NOPASSWD（部署结束后自动清除）..."
_setup_nopasswd || err "sudo NOPASSWD 配置失败"
ok "NOPASSWD 已配置"
# 注册退出钩子，无论成功或失败都清理 NOPASSWD
trap '_cleanup_nopasswd && echo "NOPASSWD 已清除"' EXIT

# ══════════════════════════════════════════════════════════════
# Step 2: 远程创建目录 & Python 环境
# ══════════════════════════════════════════════════════════════
step "Step 2  创建目录 & Python 3 环境"

log "创建部署目录 ${REMOTE_DIR} ..."
ssh_sudo "mkdir -p ${REMOTE_DIR}"
ssh_sudo "chown ${REMOTE_USER}:${REMOTE_USER} ${REMOTE_DIR}"
ssh_run  "mkdir -p ${REMOTE_DIR}/data/exports/accounts ${REMOTE_DIR}/data/exports/raw"
ok "目录已创建"

log "修复 CentOS 8 EOL yum 源（vault.centos.org）..."
ssh_sudo "bash -c \"
    if grep -q 'mirrorlist.centos.org' /etc/yum.repos.d/CentOS-*.repo 2>/dev/null; then
        sed -i 's/mirrorlist/#mirrorlist/g' /etc/yum.repos.d/CentOS-*.repo
        sed -i 's|#baseurl=http://mirror.centos.org|baseurl=http://vault.centos.org|g' /etc/yum.repos.d/CentOS-*.repo
        echo 'yum 源已切换到 vault.centos.org'
    else
        echo 'yum 源无需修改'
    fi
\""
# 彻底删除 epel 相关 repo 文件（连接外网 fedora 镜像会超时卡住）
ssh_sudo "bash -c \"
    if ls /etc/yum.repos.d/epel*.repo 2>/dev/null | grep -q .; then
        mv /etc/yum.repos.d/epel*.repo /tmp/ 2>/dev/null || true
        echo 'epel repo 已移除（避免超时）'
    else
        echo 'epel repo 已不存在，跳过'
    fi
\""
ok "yum 源处理完成"

# ── 检测/安装 python38 ────────────────────────────────────────
log "检测远程 Python 3.8+ ..."
CONDA_DIR="${REMOTE_DIR}/.conda"
PYTHON_BIN=$(ssh_run "
    for p in ${CONDA_DIR}/bin/python3 python3.12 python3.11 python3.10 python3.9 python3.8 python3 python; do
        if [ -x \"\$p\" ] || command -v \"\$p\" >/dev/null 2>&1; then
            \$p -c 'import sys; v=sys.version_info; exit(0 if v.major==3 and v.minor>=8 else 1)' 2>/dev/null && echo \$p && break
        fi
    done
" 2>/dev/null | head -1 || true)

if [ -z "${PYTHON_BIN}" ]; then
    log "安装 python38（vault.centos.org AppStream，无需外网）..."
    ssh_sudo "dnf module enable python38:3.8 -y 2>&1 | tail -2" || true
    ssh_sudo "yum install -y python38 python38-pip python38-devel gcc rsync curl \
        nss libX11 libXcomposite libXdamage libXext libXi libXrandr \
        alsa-lib atk cups-libs gtk3 \
        mesa-libgbm libxkbcommon at-spi2-atk libdrm pango cairo libxcb dbus-libs \
        2>&1 | tail -5"
    PYTHON_BIN=$(ssh_run "which python3.8 2>/dev/null || echo ''" | head -1 || true)
fi

# 如果 yum 也装不上，最后用 Miniconda（在服务器上下载）
if [ -z "${PYTHON_BIN}" ]; then
    warn "vault yum 无法安装 python38，改用 Miniconda（在服务器上下载）..."
    MINICONDA_TMP="/tmp/miniconda_installer.sh"
    # 检查是否已有 Miniconda
    CONDA_PY="${CONDA_DIR}/bin/python3"
    if ssh_run "[ -x '${CONDA_PY}' ] && ${CONDA_PY} --version" 2>/dev/null | grep -q "Python"; then
        log "Miniconda 已存在，跳过下载"
        PYTHON_BIN="${CONDA_PY}"
    else
        ssh_run "curl -fL --retry 3 \
            'https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/Miniconda3-latest-Linux-x86_64.sh' \
            -o ${MINICONDA_TMP} 2>&1 | tail -1 || \
            curl -fL --retry 3 \
            'https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh' \
            -o ${MINICONDA_TMP} 2>&1 | tail -1" \
            || err "Miniconda 下载失败，请在服务器上手动执行:
  curl -fL https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/Miniconda3-latest-Linux-x86_64.sh -o /tmp/mc.sh
  bash /tmp/mc.sh -b -p ${CONDA_DIR}"
        log "安装 Miniconda → ${CONDA_DIR} ..."
        ssh_run "bash ${MINICONDA_TMP} -b -u -p ${CONDA_DIR} && rm -f ${MINICONDA_TMP}"
        PYTHON_BIN="${CONDA_DIR}/bin/python3"
        ok "Miniconda 安装完成"
    fi
fi

[ -n "${PYTHON_BIN}" ] || err "无法获取 Python 3.8+，请联系运维"
PYTHON_VER=$(ssh_run "${PYTHON_BIN} --version 2>&1")
ok "使用 ${PYTHON_VER}（${PYTHON_BIN}）"

# ── 创建/复用虚拟环境 ─────────────────────────────────────────
log "检查虚拟环境 .venv ..."
VENV_PY="${REMOTE_DIR}/.venv/bin/python3"
VENV_PIP="${REMOTE_DIR}/.venv/bin/pip3"
NEED_REBUILD=$(ssh_run "
    # 断链检测：软链存在但目标不存在
    if [ -L '${VENV_PY}' ] && ! [ -e '${VENV_PY}' ]; then
        echo 'broken'
    elif ! [ -f '${VENV_PY}' ]; then
        echo 'missing'
    else
        echo 'ok'
    fi
" 2>/dev/null || echo "missing")

if [ "${NEED_REBUILD}" = "broken" ]; then
    warn "检测到断链 venv（Miniconda 残留），重建..."
    ssh_run "rm -rf '${REMOTE_DIR}/.venv'"
    ssh_run "cd ${REMOTE_DIR} && ${PYTHON_BIN} -m venv .venv && echo 'venv OK'"
    ok "虚拟环境已重建"
elif [ "${NEED_REBUILD}" = "missing" ]; then
    log "创建虚拟环境 .venv ..."
    ssh_run "cd ${REMOTE_DIR} && ${PYTHON_BIN} -m venv .venv && echo 'venv OK'"
    ok "虚拟环境已创建"
else
    ok "虚拟环境已存在，跳过创建"
fi

# ══════════════════════════════════════════════════════════════
# Step 3: 同步代码 & 安装依赖
# ══════════════════════════════════════════════════════════════
step "Step 3  同步代码 & 安装依赖"

log "rsync 推送代码（排除 .venv / .env / data/ 等）..."
rsync_push "${LOCAL_DIR}/" "${REMOTE_DIR}/"
ok "代码同步完成"

# 生成远程 .env（从本地读取代理和 API Key，强制 HEADLESS=true）
log "生成远程 .env ..."
LOCAL_ENV="${LOCAL_DIR}/.env"
PROXY_VAL=""; CAPSOLVER_VAL=""; TWOCAPTCHA_VAL=""
if [ -f "${LOCAL_ENV}" ]; then
    PROXY_VAL=$(grep -E '^PROXY='             "${LOCAL_ENV}" | head -1 | cut -d= -f2- || true)
    CAPSOLVER_VAL=$(grep -E '^CAPSOLVER_API_KEY=' "${LOCAL_ENV}" | head -1 | cut -d= -f2- || true)
    TWOCAPTCHA_VAL=$(grep -E '^TWOCAPTCHA_API_KEY=' "${LOCAL_ENV}" | head -1 | cut -d= -f2- || true)
fi

TMP_ENV=$(mktemp)
cat > "${TMP_ENV}" <<EOF
# 自动生成于 $(date '+%Y-%m-%d %H:%M:%S') by deploy.sh
DEFAULT_IMAP_HOST=imap.feishu.cn
DEFAULT_IMAP_PORT=993
IMAP_SEARCH_FOLDERS=INBOX,Junk,Spam

PROXY=${PROXY_VAL}

BROWSER_LOGIN_CONCURRENCY=1
API_CONCURRENCY=10

CAPSOLVER_API_KEY=${CAPSOLVER_VAL}
TWOCAPTCHA_API_KEY=${TWOCAPTCHA_VAL}

ACCOUNTS_CSV=data/accounts.csv
TOKENS_DB=data/tokens.db
EXPORTS_DIR=data/exports

# 服务器必须无头模式
HEADLESS=true
VERIFICATION_CODE_TIMEOUT=120
EOF
scp_put "${TMP_ENV}" "${REMOTE_DIR}/.env"
rm -f "${TMP_ENV}"
ok ".env 已写入（HEADLESS=true）"

# 同步 accounts.csv（如本地有）
if [ -f "${LOCAL_DIR}/data/accounts.csv" ]; then
    scp_put "${LOCAL_DIR}/data/accounts.csv" "${REMOTE_DIR}/data/accounts.csv"
    ok "accounts.csv 已同步"
else
    ssh_run "[ -f ${REMOTE_DIR}/data/accounts.csv ] || \
        echo 'email,imap_password,imap_host,imap_port' > ${REMOTE_DIR}/data/accounts.csv"
    warn "accounts.csv 未找到，已创建空模板，可通过 Web UI 上传"
fi

# ── 安装 Python 依赖（检查是否已安装，跳过不必要的重装）──────
log "检查 Python 依赖..."
NEED_PIP=$(ssh_run "
    cd ${REMOTE_DIR}
    # 检查核心包是否已安装
    if ${VENV_PIP} show fastapi patchright uvicorn openpyxl >/dev/null 2>&1; then
        echo 'installed'
    else
        echo 'missing'
    fi
" 2>/dev/null || echo "missing")

if [ "${NEED_PIP}" = "installed" ]; then
    ok "Python 依赖已安装，跳过（如需强制更新请删除 .venv 后重跑）"
else
    log "安装 Python 依赖..."
    ssh_run "cd ${REMOTE_DIR} && ${VENV_PIP} install --upgrade pip -q 2>&1 | tail -1"
    ssh_run "cd ${REMOTE_DIR} && ${VENV_PIP} install -r requirements.txt 2>&1 | tail -5"
    ok "Python 依赖安装完成"
fi

# ── 安装 patchright Chromium（检查是否已存在）──────────────────
log "检查 patchright Chromium ..."
# Playwright/patchright 默认把 Chromium 装到 ~/.cache/ms-playwright/
CHROMIUM_EXISTS=$(ssh_run "
    if find \"\${HOME}/.cache/ms-playwright\" -name 'chrome' -type f 2>/dev/null | grep -q .; then
        echo 'yes'
    else
        echo 'no'
    fi
" 2>/dev/null || echo "no")

if [ "${CHROMIUM_EXISTS}" = "yes" ]; then
    ok "patchright Chromium 已安装，跳过"
else
    log "安装 patchright Chromium（可能需要 2-5 分钟）..."
    ssh_run "cd ${REMOTE_DIR} && ${VENV_PY} -m patchright install chromium 2>&1 | tail -5" \
        || warn "patchright chromium 安装异常，浏览器登录功能可能不可用"
    ok "Chromium 已安装"
fi

# ══════════════════════════════════════════════════════════════
# Step 4: 打通网络 & 创建 systemd 服务
# ══════════════════════════════════════════════════════════════
step "Step 4  网络 & systemd 服务"

log "开放端口 ${APP_PORT} ..."
ssh_run "
PASS='${REMOTE_PASS}'
PORT=${APP_PORT}
if command -v firewall-cmd &>/dev/null && systemctl is-active firewalld &>/dev/null 2>&1; then
    echo \"\$PASS\" | sudo -S -p '' firewall-cmd --permanent --add-port=\${PORT}/tcp
    echo \"\$PASS\" | sudo -S -p '' firewall-cmd --reload
    echo 'firewalld: 端口已开放'
elif command -v iptables &>/dev/null; then
    echo \"\$PASS\" | sudo -S -p '' iptables -C INPUT -p tcp --dport \${PORT} -j ACCEPT 2>/dev/null \
        || echo \"\$PASS\" | sudo -S -p '' iptables -I INPUT -p tcp --dport \${PORT} -j ACCEPT
    echo \"\$PASS\" | sudo -S -p '' service iptables save 2>/dev/null || true
    echo 'iptables: 端口已开放'
else
    echo '未检测到防火墙，跳过'
fi
"
ok "端口 ${APP_PORT} 已处理"

log "创建 systemd 服务 ${SERVICE_NAME} ..."
TMP_SVC=$(mktemp)
cat > "${TMP_SVC}" <<EOF
[Unit]
Description=Cursor Account Manager Web UI
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=${REMOTE_USER}
WorkingDirectory=${REMOTE_DIR}
# 启动前先拉起 Xvfb 虚拟显示器（让 Chromium 以非 headless 模式运行，绕过 Cloudflare 检测）
ExecStartPre=/bin/bash -c 'pkill Xvfb 2>/dev/null; Xvfb :99 -screen 0 1920x1080x24 -ac &'
ExecStart=${REMOTE_DIR}/.venv/bin/python3 -m cam web --host 0.0.0.0 --port ${APP_PORT}
ExecStopPost=/bin/bash -c 'pkill Xvfb 2>/dev/null; true'
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment="PYTHONUNBUFFERED=1"
Environment="DISPLAY=:99"
Environment="HEADLESS=false"

[Install]
WantedBy=multi-user.target
EOF

scp_put "${TMP_SVC}" "/tmp/${SERVICE_NAME}.service"
rm -f "${TMP_SVC}"

ssh_sudo "cp /tmp/${SERVICE_NAME}.service /etc/systemd/system/${SERVICE_NAME}.service"
ssh_sudo "systemctl daemon-reload"
ssh_sudo "systemctl enable ${SERVICE_NAME}"
ssh_sudo "systemctl restart ${SERVICE_NAME}"
ok "systemd 服务已启动"

# 等待 5s 检查状态
sleep 5
SVC_STATUS=$(ssh_run "systemctl is-active ${SERVICE_NAME} 2>/dev/null || echo inactive")
if [ "${SVC_STATUS}" = "active" ]; then
    ok "服务运行正常（active）"
else
    warn "服务状态: ${SVC_STATUS}，打印最近日志："
    echo "────────────────────────────────────────────────────"
    ssh_run "journalctl -u ${SERVICE_NAME} -n 50 --no-pager 2>/dev/null" || true
    echo "────────────────────────────────────────────────────"
    err "服务启动失败，请根据上方日志排查"
fi

# ══════════════════════════════════════════════════════════════
# Step 5: 输出访问信息
# ══════════════════════════════════════════════════════════════
step "Step 5  部署完成"

echo
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║       Cursor Account Manager 部署成功！              ║${NC}"
echo -e "${GREEN}${BOLD}╠══════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}${BOLD}║${NC}  Web UI:    ${BOLD}http://${REMOTE_HOST}:${APP_PORT}/${NC}"
echo -e "${GREEN}${BOLD}║${NC}  SSH:       ssh ${REMOTE_USER}@${REMOTE_HOST}"
echo -e "${GREEN}${BOLD}║${NC}  部署目录:  ${REMOTE_DIR}"
echo -e "${GREEN}${BOLD}╠══════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}${BOLD}║${NC}  常用运维命令（登录远程服务器后执行）："
echo -e "${GREEN}${BOLD}║${NC}    查看日志:  journalctl -u ${SERVICE_NAME} -f"
echo -e "${GREEN}${BOLD}║${NC}    重启服务:  sudo systemctl restart ${SERVICE_NAME}"
echo -e "${GREEN}${BOLD}║${NC}    停止服务:  sudo systemctl stop ${SERVICE_NAME}"
echo -e "${GREEN}${BOLD}║${NC}  更新代码（从本机重跑）:"
echo -e "${GREEN}${BOLD}║${NC}    bash deploy.sh"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo

# 自动 tail 日志，Ctrl+C 退出
echo -e "${BOLD}正在连接服务器查看实时日志（Ctrl+C 退出）...${NC}"
echo "────────────────────────────────────────────────────"
sshpass -p "${REMOTE_PASS}" ssh ${SSH_OPTS} "${REMOTE_USER}@${REMOTE_HOST}" \
    "journalctl -u ${SERVICE_NAME} -f --no-pager" || true
