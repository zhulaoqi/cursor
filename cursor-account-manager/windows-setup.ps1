# =============================================================================
# windows-setup.ps1
# 在 Windows 机器上部署 cursor-account-manager
#
# 由 deploy-to-windows.sh 远程调用，也可在 Windows 本地手动运行：
#   powershell -ExecutionPolicy Bypass -File windows-setup.ps1 `
#     -PackagePath "C:\path\to\cam-xxx.zip" `
#     -DeployDir "D:\deploy\cursor-account-manager"
# =============================================================================

param(
    [string]$PackagePath = "",
    [string]$DeployDir   = "D:\deploy\cursor-account-manager",
    [switch]$SkipChrome  = $false,   # 保留参数兼容性，实际不再安装 Chrome
    [switch]$StartServer = $false
)

# 强制 UTF-8 输出，避免 SSH 传输时中文乱码
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding            = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------- 日志函数 ----------
function Log-Info  { param($msg) Write-Host "[INFO]  $msg" -ForegroundColor Cyan }
function Log-OK    { param($msg) Write-Host "[OK]    $msg" -ForegroundColor Green }
function Log-Warn  { param($msg) Write-Host "[WARN]  $msg" -ForegroundColor Yellow }
function Log-Error { param($msg) Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Log-Step  { param($msg) Write-Host "`n========== $msg ==========" -ForegroundColor Magenta }

function Remove-PathRobust {
    param(
        [string]$Path,
        [int]$Retry = 3
    )
    if (-not (Test-Path $Path)) { return }

    for ($i = 1; $i -le $Retry; $i++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            if (-not (Test-Path $Path)) { return }
        } catch {
            # 目录已不存在时视为成功，避免无意义 WARN
            if (-not (Test-Path $Path)) { return }
            $errMsg = [string]$_.Exception.Message
            if ($errMsg -match "(?i)cannot find the file specified") { return }
            Log-Warn "删除目录失败（第 $i/$Retry 次）: $Path -> $($_.Exception.Message)"
            Start-Sleep -Seconds 1
        }
    }

    # 兜底：使用 cmd 的 rmdir，处理部分 Win32 遍历异常
    cmd /d /c "rmdir /s /q ""$Path""" 2>$null | Out-Null
    if (Test-Path $Path) {
        Log-Error "删除目录失败（重试 + rmdir 兜底后仍存在）: $Path"
    }
}

function Copy-DirRobust {
    param(
        [string]$Source,
        [string]$Destination,
        [int]$Retry = 2
    )
    if (-not (Test-Path $Source)) { return }
    if (-not (Test-Path $Destination)) {
        New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    }

    for ($i = 1; $i -le $Retry; $i++) {
        robocopy $Source $Destination /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
        $rc = $LASTEXITCODE
        if ($rc -le 7) { return }
        Log-Warn "目录复制失败（第 $i/$Retry 次，robocopy exit=$rc）：$Source -> $Destination"
        Start-Sleep -Seconds 1
    }
    Log-Error "目录复制失败（重试后仍失败）：$Source -> $Destination"
}

# ---------- 刷新 PATH ----------
function Refresh-Path {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path","User")
}

# ---------- 依赖自检 ----------
function Test-CamDependencies {
    param(
        [string]$PythonExe,
        [string]$BrowsersDir
    )
    Log-Step "依赖自检"

    if (-not (Test-Path $PythonExe)) {
        Log-Error "依赖自检失败：Python 不存在 -> $PythonExe"
    }

    # 核心 Python 依赖导入检查（避免运行期才报 ModuleNotFoundError）
    $checkScript = @"
import importlib, sys
mods = ["click", "requests", "dotenv", "patchright", "fastapi", "uvicorn", "pymysql", "cryptography", "openpyxl", "fitz", "PIL"]
missing = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception:
        missing.append(m)
if missing:
    print("MISSING:" + ",".join(missing))
    sys.exit(2)
print("OK")
"@
    $checkFile = Join-Path $env:TEMP "cam_dep_check.py"
    [System.IO.File]::WriteAllText($checkFile, $checkScript, [System.Text.Encoding]::UTF8)
    $pyOut = & $PythonExe $checkFile 2>&1
    $pyEc = $LASTEXITCODE
    Remove-Item $checkFile -Force -ErrorAction SilentlyContinue
    if ($pyEc -ne 0) {
        Log-Error "依赖自检失败：Python 包缺失或损坏 -> $pyOut"
    }
    Log-OK "Python 包导入检查通过"

    # Patchright 浏览器可执行文件检查（统一共享目录）
    if (-not (Test-Path $BrowsersDir)) {
        Log-Error "依赖自检失败：浏览器目录不存在 -> $BrowsersDir"
    }
    $pwCheckScript = @"
import os, sys
from patchright.sync_api import sync_playwright
try:
    with sync_playwright() as p:
        exe = p.chromium.executable_path
    if not exe or not os.path.exists(exe):
        print("MISSING_EXECUTABLE:" + str(exe))
        sys.exit(3)
    print("EXECUTABLE_PATH:" + exe)
except Exception as e:
    print("CHECK_FAILED:" + str(e))
    sys.exit(4)
"@
    $pwCheckFile = Join-Path $env:TEMP "cam_pw_check.py"
    [System.IO.File]::WriteAllText($pwCheckFile, $pwCheckScript, [System.Text.Encoding]::UTF8)
    $pwOut = cmd /d /c "set PLAYWRIGHT_BROWSERS_PATH=$BrowsersDir && `"$PythonExe`" `"$pwCheckFile`"" 2>&1
    $pwEc = $LASTEXITCODE
    Remove-Item $pwCheckFile -Force -ErrorAction SilentlyContinue
    if ($pwEc -ne 0) {
        Log-Error "依赖自检失败：Patchright 浏览器不可用 -> $pwOut"
    }
    Log-OK "Patchright 浏览器可执行文件检查通过: $pwOut"
}

# ---------- winget 安装（幂等）----------
function Install-Via-Winget {
    param([string]$Id, [string]$Name, [string]$ExePath = "")
    Log-Info "检查 $Name ..."
    if ($ExePath -and (Test-Path $ExePath)) { Log-OK "$Name 已安装，跳过"; return }
    Log-Info "安装 $Name ..."
    winget install --id $Id --silent --accept-package-agreements --accept-source-agreements `
        --disable-interactivity --source winget 2>&1 | Out-Host
    if ($LASTEXITCODE -notin @(0, -1978335189)) { Log-Error "安装 $Name 失败，退出码: $LASTEXITCODE" }
    Log-OK "$Name 安装完成"
}

# =====================================================================
Log-Step "Step 1: 检查 Python"
# =====================================================================
$pythonCmd = ""
Refresh-Path

# 优先直接找真实安装路径（跳过 Windows 商店占位符）
$pythonPaths = @(
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
    "$env:ProgramFiles\Python312\python.exe",
    "$env:ProgramFiles\Python311\python.exe",
    "C:\Python312\python.exe",
    "C:\Python311\python.exe"
)
foreach ($p in $pythonPaths) {
    if (Test-Path $p) { $pythonCmd = $p; break }
}

# 再尝试 PATH 里的命令（排除商店假程序）
if (-not $pythonCmd) {
    foreach ($candidate in @("python3", "py", "python")) {
        $cmdObj = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $cmdObj) { continue }
        # 商店占位符路径含 WindowsApps
        if ($cmdObj.Source -like "*WindowsApps*") { continue }
        try {
            $ver = & $cmdObj.Source --version 2>&1
            if ($ver -match "Python 3\.(1[0-9]|[89])") { $pythonCmd = $cmdObj.Source; break }
        } catch { continue }
    }
}

# 仍未找到 → winget 安装
if (-not $pythonCmd) {
    Install-Via-Winget -Id "Python.Python.3.12" -Name "Python 3.12" `
        -ExePath "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    Refresh-Path
    $pythonCmd = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
}

if (-not (Test-Path $pythonCmd)) { Log-Error "找不到 Python，请手动安装 3.10+ 后重试" }
Log-OK "Python: $pythonCmd → $(& $pythonCmd --version 2>&1)"

# =====================================================================
Log-Step "Step 2: 停止已运行的服务"
# =====================================================================
$oldProcs = Get-WmiObject Win32_Process |
    Where-Object { $_.CommandLine -like "*-m cam web*" -or $_.CommandLine -like "*-m`"cam`"web*" }
if ($oldProcs) {
    foreach ($proc in $oldProcs) {
        Log-Info "停止旧进程 PID $($proc.ProcessId): $($proc.CommandLine)"
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep 2
    Log-OK "旧服务已停止"
} else {
    Log-OK "没有正在运行的服务，跳过"
}

# =====================================================================
Log-Step "Step 3: 解压代码"
# =====================================================================
$deployParent = Split-Path $DeployDir -Parent
if (-not (Test-Path $deployParent)) {
    New-Item -ItemType Directory -Path $deployParent -Force | Out-Null
}

$backup = $null
if ($PackagePath -and (Test-Path $PackagePath)) {
    Log-Info "解压并覆盖代码（保留 data/.env）: $PackagePath"
    if (Test-Path $DeployDir) {
        Log-Info "备份旧 .env ..."
        do {
            $backup = "$deployParent\.cam_backup_$(Get-Date -Format yyyyMMdd_HHmmss_fff)_$([System.Guid]::NewGuid().ToString('N').Substring(0,6))"
        } while (Test-Path $backup)
        New-Item -ItemType Directory -Path $backup -Force | Out-Null
        if (Test-Path "$DeployDir\.env") { Copy-Item "$DeployDir\.env" $backup -Force }
    } else {
        New-Item -ItemType Directory -Path $DeployDir -Force | Out-Null
    }

    $extractTmp = Join-Path $deployParent ".cam_extract_$([System.Guid]::NewGuid().ToString('N').Substring(0,8))"
    New-Item -ItemType Directory -Path $extractTmp -Force | Out-Null
    Expand-Archive -Path $PackagePath -DestinationPath $extractTmp -Force

    $srcDir = Join-Path $extractTmp "cursor-account-manager"
    if (-not (Test-Path $srcDir)) { $srcDir = $extractTmp }

    # 仅覆盖代码，保留 data/.env（避免不必要删除导致告警与状态抖动）
    robocopy $srcDir $DeployDir /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP /XD data /XF .env | Out-Null
    $rc = $LASTEXITCODE
    if ($rc -gt 7) {
        Remove-PathRobust -Path $extractTmp
        Log-Error "代码覆盖失败（robocopy exit=$rc）"
    }
    Remove-PathRobust -Path $extractTmp

    Log-OK "代码解压到: $DeployDir"
} else {
    if (-not (Test-Path $DeployDir)) { Log-Error "未提供代码包且目标目录不存在: $DeployDir" }
    Log-Warn "未提供代码包，使用已有目录: $DeployDir"
}

Set-Location $DeployDir
Log-OK "工作目录: $DeployDir"

# =====================================================================
Log-Step "Step 4: 生成 .env 配置文件"
# =====================================================================
# 合并策略：
#   系统配置项（并发数、超时、headless 等）→ 始终使用新模板默认值，确保每次部署都能生效
#   用户密钥类配置（PROXY / CAPSOLVER_API_KEY / TWOCAPTCHA_API_KEY / BI / ALERT）→ 从旧 .env 中读取并保留
#   用户自定义路径 / IMAP 等 → 从旧 .env 中读取并保留
# 这样新增或修改系统配置（如 BROWSER_LOGIN_CONCURRENCY）自动生效，密钥不会丢失。
# ─────────────────────────────────────────────────────────────────────
$envFile   = "$DeployDir\.env"
$backupEnv = if ($backup) { "$backup\.env" } else { $null }

# ── Step 4a: 从旧 .env 读取需要保留的用户值 ────────────────────────
# 这些 key 用户可能已填写，部署时保留；其余 key 使用新模板默认值。
$userKeys = @(
    "PROXY",
    "CAPSOLVER_API_KEY",
    "TWOCAPTCHA_API_KEY",
    "DEFAULT_IMAP_HOST",
    "DEFAULT_IMAP_PORT",
    "IMAP_SEARCH_FOLDERS",
    "BI_SYNC_ENABLE",
    "BI_SYNC_DB_URL",
    "BI_SYNC_DB_USERNAME",
    "BI_SYNC_DB_PASSWORD",
    "BI_SYNC_BIZ_TZ",
    "BI_SYNC_CRON",
    "BI_SYNC_RETRY_TIMES",
    "BI_SYNC_ACCOUNT_TIMEOUT_SEC",
    "BI_SYNC_DB_QUERY_TIMEOUT_SEC",
    "BI_SYNC_BATCH_SIZE",
    "BI_SYNC_LOCK_FILE",
    "LEDGER_DB_HOST",
    "LEDGER_DB_PORT",
    "LEDGER_DB_USER",
    "LEDGER_DB_PASSWORD",
    "LEDGER_DB_NAME",
    "LEDGER_DB_POOL_MAX_CONNECTIONS",
    "LEDGER_DB_POOL_MIN_CACHED",
    "LEDGER_DB_POOL_MAX_CACHED",
    "LEDGER_DB_CONNECT_TIMEOUT_SEC",
    "LEDGER_DB_READ_TIMEOUT_SEC",
    "LEDGER_DB_WRITE_TIMEOUT_SEC",
    "LEDGER_DB_CONNECT_RETRY_TIMES",
    "LEDGER_DB_CONNECT_RETRY_BACKOFF_SEC",
    "USAGE_SNAPSHOT_ENABLE",
    "USAGE_PERIODIC_INTERVAL_HOURS",
    "USAGE_BOOTSTRAP_STALE_HOURS",
    "USAGE_SNAPSHOT_CONCURRENCY",
    "USAGE_PERIODIC_RETRY_MINUTES",
    "USAGE_PERIODIC_MAX_ATTEMPTS_PER_SLOT",
    "USAGE_PRE_RESET_SCAN_INTERVAL_MIN",
    "USAGE_PRE_RESET_WINDOW_START_MIN",
    "USAGE_PRE_RESET_TARGET_OFFSET_MIN",
    "USAGE_PRE_RESET_WINDOW_END_MIN",
    "USAGE_LOW_THRESHOLD_PCT",
    "USAGE_CYCLE_CONTINUITY_TOLERANCE_HOURS",
    "USAGE_PERIODIC_LOCK_FILE",
    "USAGE_PRE_RESET_LOCK_FILE",
    "USAGE_ACCOUNT_LOCK_DIR",
    "USAGE_ACCOUNT_LOCK_TIMEOUT_SEC",
    "USAGE_AUTH_BREAKER_MIN_SAMPLES",
    "USAGE_AUTH_BREAKER_FAILURE_RATIO",
    "USAGE_AUTH_BREAKER_COOLDOWN_MIN",
    "USAGE_AUTH_BREAKER_WINDOW_SIZE",
    "USAGE_AUTH_BREAKER_WINDOW_MIN",
    "USAGE_AUTH_QUARANTINE_FAILURES",
    "DEVIN_SCHEDULE_ENABLE",
    "DEVIN_DAILY_AT",
    "DEVIN_CONCURRENCY",
    "DEVIN_CODE_SENDER_KEYWORDS",
    "DEVIN_PROXY",
    "ALERT_BOT_ENABLE",
    "ALERT_BOT_PROVIDER",
    "ALERT_BOT_CLIENT_ID",
    "ALERT_BOT_SECRET",
    "ALERT_TO_EMAILS"
)
$userVals = @{}
if ($backupEnv -and (Test-Path $backupEnv)) {
    $oldLines = [System.IO.File]::ReadAllLines($backupEnv, [System.Text.Encoding]::UTF8)
    foreach ($line in $oldLines) {
        foreach ($key in $userKeys) {
            if ($line -match "^$key=(.*)") {
                $userVals[$key] = $Matches[1]
                break
            }
        }
    }
    Log-Info "  从旧 .env 读取用户配置: $($userVals.Keys -join ', ')"
}

# ── Step 4b: 写入新 .env（模板 + 用户值回填）────────────────────────
function Get-UserVal($key, $default) {
    if ($userVals.ContainsKey($key) -and $userVals[$key] -ne "") { return $userVals[$key] }
    return $default
}

if (-not (Test-Path "D:\")) {
    Log-Error "未检测到 D 盘。Chrome profiles 必须放在 D:\cam-data\chrome-profiles，禁止再写 C 盘。"
}
$chromeProfilesDir = "D:\cam-data\chrome-profiles"

$proxyLine     = if ($userVals["PROXY"]) { "PROXY=$($userVals['PROXY'])" } else { "# PROXY=http://user:pass@host:port" }
$capsolverLine = if ($userVals["CAPSOLVER_API_KEY"]) { "CAPSOLVER_API_KEY=$($userVals['CAPSOLVER_API_KEY'])" } else { "# CAPSOLVER_API_KEY=" }
$twocapLine    = if ($userVals["TWOCAPTCHA_API_KEY"]) { "TWOCAPTCHA_API_KEY=$($userVals['TWOCAPTCHA_API_KEY'])" } else { "# TWOCAPTCHA_API_KEY=" }

$envContent = @(
    "# ─── IMAP 默认配置 ───",
    "DEFAULT_IMAP_HOST=$(Get-UserVal 'DEFAULT_IMAP_HOST' 'imap.feishu.cn')",
    "DEFAULT_IMAP_PORT=$(Get-UserVal 'DEFAULT_IMAP_PORT' '993')",
    "IMAP_SEARCH_FOLDERS=$(Get-UserVal 'IMAP_SEARCH_FOLDERS' 'INBOX,Junk,Spam')",
    "# 邮箱组：每次部署都按这里的名单覆盖，不保留旧 .env。授权码仍存在每个账号上。全部改完后把账号名单改成 *@eclicktech.com.cn",
    "IMAP_GROUP_MAILBOXES=eclicktech.com.cn=kinch.zhu@eclicktech.com.cn",
    "IMAP_GROUP_MAILBOX_ACCOUNTS=cursor97@eclicktech.com.cn,cursor151@eclicktech.com.cn,cursor144@eclicktech.com.cn,cursor162@eclicktech.com.cn,cursor154@eclicktech.com.cn,cursor85@eclicktech.com.cn,cursor78@eclicktech.com.cn,cursor59@eclicktech.com.cn,cursor132@eclicktech.com.cn,cursor119@eclicktech.com.cn,cursor102@eclicktech.com.cn,cursor98@eclicktech.com.cn,cursor182@eclicktech.com.cn",
    "",
    "# ─── 代理（保留上次部署的值，如需修改请直接编辑此文件）───",
    $proxyLine,
    "",
    "# ─── 并发控制（16GB 专用机默认；可在 .env 覆盖）───",
    "BROWSER_LOGIN_CONCURRENCY=$(Get-UserVal 'BROWSER_LOGIN_CONCURRENCY' '8')",
    "INVOICE_DOWNLOAD_CONCURRENCY=$(Get-UserVal 'INVOICE_DOWNLOAD_CONCURRENCY' '10')",
    "INVOICE_ACTIVE_CONTEXT_LIMIT=$(Get-UserVal 'INVOICE_ACTIVE_CONTEXT_LIMIT' '6')",
    "API_CONCURRENCY=$(Get-UserVal 'API_CONCURRENCY' '30')",
    "WEB_FETCH_MAX_WORKERS=$(Get-UserVal 'WEB_FETCH_MAX_WORKERS' '0')",
    "",
    "# ─── 账期净支出抓取重试 ───",
    "BILLING_LEDGER_CONCURRENCY=$(Get-UserVal 'BILLING_LEDGER_CONCURRENCY' '3')",
    "BILLING_LEDGER_RETRY_TIMES=$(Get-UserVal 'BILLING_LEDGER_RETRY_TIMES' '2')",
    "BILLING_LEDGER_RETRY_BACKOFF_SEC=$(Get-UserVal 'BILLING_LEDGER_RETRY_BACKOFF_SEC' '3')",
    "BILLING_LEDGER_REFRESH_ENABLE=$(Get-UserVal 'BILLING_LEDGER_REFRESH_ENABLE' 'true')",
    "BILLING_LEDGER_REFRESH_CRON=$(Get-UserVal 'BILLING_LEDGER_REFRESH_CRON' '0 5 * * *')",
    "BILLING_LEDGER_REFRESH_LOCK_FILE=$(Get-UserVal 'BILLING_LEDGER_REFRESH_LOCK_FILE' '/tmp/cam_billing_ledger_refresh.lock')",
    "",
    "# ─── 账期数据库与用量监控（密码仅保留旧 .env，首次部署需手工填写）───",
    "LEDGER_DB_HOST=$(Get-UserVal 'LEDGER_DB_HOST' '172.30.11.103')",
    "LEDGER_DB_PORT=$(Get-UserVal 'LEDGER_DB_PORT' '3306')",
    "LEDGER_DB_USER=$(Get-UserVal 'LEDGER_DB_USER' 'aicoding_admin')",
    "LEDGER_DB_PASSWORD=$(Get-UserVal 'LEDGER_DB_PASSWORD' '')",
    "LEDGER_DB_NAME=$(Get-UserVal 'LEDGER_DB_NAME' 'aicoding')",
    "LEDGER_DB_POOL_MAX_CONNECTIONS=$(Get-UserVal 'LEDGER_DB_POOL_MAX_CONNECTIONS' '8')",
    "LEDGER_DB_POOL_MIN_CACHED=$(Get-UserVal 'LEDGER_DB_POOL_MIN_CACHED' '1')",
    "LEDGER_DB_POOL_MAX_CACHED=$(Get-UserVal 'LEDGER_DB_POOL_MAX_CACHED' '4')",
    "LEDGER_DB_CONNECT_TIMEOUT_SEC=$(Get-UserVal 'LEDGER_DB_CONNECT_TIMEOUT_SEC' '10')",
    "LEDGER_DB_READ_TIMEOUT_SEC=$(Get-UserVal 'LEDGER_DB_READ_TIMEOUT_SEC' '30')",
    "LEDGER_DB_WRITE_TIMEOUT_SEC=$(Get-UserVal 'LEDGER_DB_WRITE_TIMEOUT_SEC' '30')",
    "LEDGER_DB_CONNECT_RETRY_TIMES=$(Get-UserVal 'LEDGER_DB_CONNECT_RETRY_TIMES' '3')",
    "LEDGER_DB_CONNECT_RETRY_BACKOFF_SEC=$(Get-UserVal 'LEDGER_DB_CONNECT_RETRY_BACKOFF_SEC' '2')",
    # 默认开启；若旧 .env 未配置该键，首次部署也要启动用量调度（需同步配置 LEDGER_DB_PASSWORD）
    "USAGE_SNAPSHOT_ENABLE=$(Get-UserVal 'USAGE_SNAPSHOT_ENABLE' 'true')",
    "USAGE_PERIODIC_INTERVAL_HOURS=$(Get-UserVal 'USAGE_PERIODIC_INTERVAL_HOURS' '24')",
    "USAGE_BOOTSTRAP_STALE_HOURS=$(Get-UserVal 'USAGE_BOOTSTRAP_STALE_HOURS' '36')",
    "USAGE_SNAPSHOT_CONCURRENCY=$(Get-UserVal 'USAGE_SNAPSHOT_CONCURRENCY' '3')",
    "USAGE_PERIODIC_RETRY_MINUTES=$(Get-UserVal 'USAGE_PERIODIC_RETRY_MINUTES' '30')",
    "USAGE_PERIODIC_MAX_ATTEMPTS_PER_SLOT=$(Get-UserVal 'USAGE_PERIODIC_MAX_ATTEMPTS_PER_SLOT' '3')",
    "USAGE_PRE_RESET_SCAN_INTERVAL_MIN=$(Get-UserVal 'USAGE_PRE_RESET_SCAN_INTERVAL_MIN' '15')",
    "USAGE_PRE_RESET_WINDOW_START_MIN=$(Get-UserVal 'USAGE_PRE_RESET_WINDOW_START_MIN' '360')",
    "USAGE_PRE_RESET_TARGET_OFFSET_MIN=$(Get-UserVal 'USAGE_PRE_RESET_TARGET_OFFSET_MIN' '180')",
    "USAGE_PRE_RESET_WINDOW_END_MIN=$(Get-UserVal 'USAGE_PRE_RESET_WINDOW_END_MIN' '30')",
    "USAGE_LOW_THRESHOLD_PCT=$(Get-UserVal 'USAGE_LOW_THRESHOLD_PCT' '30')",
    "USAGE_CYCLE_CONTINUITY_TOLERANCE_HOURS=$(Get-UserVal 'USAGE_CYCLE_CONTINUITY_TOLERANCE_HOURS' '48')",
    "USAGE_PERIODIC_LOCK_FILE=$(Get-UserVal 'USAGE_PERIODIC_LOCK_FILE' 'data/cam_usage_periodic.lock')",
    "USAGE_PRE_RESET_LOCK_FILE=$(Get-UserVal 'USAGE_PRE_RESET_LOCK_FILE' 'data/cam_usage_pre_reset.lock')",
    "USAGE_ACCOUNT_LOCK_DIR=$(Get-UserVal 'USAGE_ACCOUNT_LOCK_DIR' 'data/usage-account-locks')",
    "USAGE_ACCOUNT_LOCK_TIMEOUT_SEC=$(Get-UserVal 'USAGE_ACCOUNT_LOCK_TIMEOUT_SEC' '5')",
    "USAGE_AUTH_BREAKER_MIN_SAMPLES=$(Get-UserVal 'USAGE_AUTH_BREAKER_MIN_SAMPLES' '20')",
    "USAGE_AUTH_BREAKER_FAILURE_RATIO=$(Get-UserVal 'USAGE_AUTH_BREAKER_FAILURE_RATIO' '0.50')",
    "USAGE_AUTH_BREAKER_COOLDOWN_MIN=$(Get-UserVal 'USAGE_AUTH_BREAKER_COOLDOWN_MIN' '30')",
    "USAGE_AUTH_BREAKER_WINDOW_SIZE=$(Get-UserVal 'USAGE_AUTH_BREAKER_WINDOW_SIZE' '50')",
    "USAGE_AUTH_BREAKER_WINDOW_MIN=$(Get-UserVal 'USAGE_AUTH_BREAKER_WINDOW_MIN' '10')",
    "USAGE_AUTH_QUARANTINE_FAILURES=$(Get-UserVal 'USAGE_AUTH_QUARANTINE_FAILURES' '3')",
    "",
    "# ─── Turnstile 兜底求解器 ───",
    $capsolverLine,
    $twocapLine,
    "",
    "# ─── 数据路径 ───",
    "ACCOUNTS_CSV=D:\cam-data\accounts.csv",
    "TOKENS_DB=D:\cam-data\tokens.db",
    "EXPORTS_DIR=data/exports",
    "CAM_CHROME_PROFILES_DIR=$chromeProfilesDir",
    "",
    "# ─── 浏览器行为 ───",
    "HEADLESS=false",
    "VERIFICATION_CODE_TIMEOUT=120",
    "",
    "# ─── BI 每日同步（StarRocks） ───",
    "BI_SYNC_ENABLE=true",
    "BI_SYNC_DB_URL=jdbc:mysql://fe-c-70afd013dd6a6a8e-internal.starrocks.aliyuncs.com:9030/dataeye_aiboard",
    "BI_SYNC_DB_USERNAME=pro",
    'BI_SYNC_DB_PASSWORD="76x1Jx#1x35FGxY"',
    "BI_SYNC_BIZ_TZ=Asia/Shanghai",
    "BI_SYNC_CRON=30 1 * * *",
    "BI_SYNC_RETRY_TIMES=3",
    "BI_SYNC_ACCOUNT_TIMEOUT_SEC=600",
    "BI_SYNC_DB_QUERY_TIMEOUT_SEC=120",
    "BI_SYNC_BATCH_SIZE=5000",
    "BI_SYNC_LOCK_FILE=data/cam_bi_sync.lock",
    "",
    "# ─── 套餐/按量付费 每日定时刷新 ───",
    "SPENDING_REFRESH_ENABLE=true",
    "SPENDING_REFRESH_CRON=0 3 * * *",
    "SPENDING_REFRESH_LOCK_FILE=/tmp/cam_spending_refresh.lock",
    "SPENDING_REFRESH_ALERT_ENABLE=true",
    "SPENDING_REFRESH_CONCURRENCY=$(Get-UserVal 'SPENDING_REFRESH_CONCURRENCY' '4')",
    "",
    "# ─── Devin 平台（每日凌晨登录采集套餐/用量，飞书播报）───",
    "DEVIN_SCHEDULE_ENABLE=$(Get-UserVal 'DEVIN_SCHEDULE_ENABLE' 'true')",
    "DEVIN_DAILY_AT=$(Get-UserVal 'DEVIN_DAILY_AT' '02:30')",
    "DEVIN_CONCURRENCY=$(Get-UserVal 'DEVIN_CONCURRENCY' '3')",
    "DEVIN_CODE_SENDER_KEYWORDS=$(Get-UserVal 'DEVIN_CODE_SENDER_KEYWORDS' 'devin,cognition,windsurf')",
    "",
    "# ─── 告警机器人（邮件接收人逗号分隔） ───",
    "ALERT_BOT_ENABLE=true",
    "ALERT_BOT_PROVIDER=feishu",
    "ALERT_BOT_CLIENT_ID=cli_a933b8d6d7b81bc3",
    "ALERT_BOT_SECRET=hDkboxuFdSm2uledMDCSJeffeC7I5UUR",
    "ALERT_TO_EMAILS=kinch.zhu@eclicktech.com.cn"
)
[System.IO.File]::WriteAllLines($envFile, $envContent, [System.Text.Encoding]::UTF8)
if ($backupEnv -and (Test-Path $backupEnv)) {
    Log-OK ".env 已更新（系统配置取新默认值，用户密钥已保留）"
} else {
    Log-OK ".env 已创建（首次部署，请按需编辑 PROXY / API KEY）"
}

# =====================================================================
Log-Step "Step 5: 创建 Python 虚拟环境"
# =====================================================================
$venvDir    = "$DeployDir\.venv"
$venvPython = "$venvDir\Scripts\python.exe"
$venvPip    = "$venvDir\Scripts\pip.exe"

if (-not (Test-Path $venvPython)) {
    Log-Info "创建虚拟环境 → $venvDir ..."
    & $pythonCmd -m venv $venvDir 2>&1 | Out-Host
    Log-OK "虚拟环境创建完成"
} else {
    Log-OK "虚拟环境已存在，跳过"
}

# =====================================================================
Log-Step "Step 6: 安装 Python 依赖"
# =====================================================================
$pwBrowsersDir = "$DeployDir\data\ms-playwright"
if (-not (Test-Path $pwBrowsersDir)) { New-Item -ItemType Directory -Path $pwBrowsersDir -Force | Out-Null }

Log-Info "升级 pip ..."
& $venvPython -m pip install --upgrade pip --quiet 2>&1 | Out-Host

Log-Info "安装 requirements.txt ..."
$prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
& $venvPip install -r "$DeployDir\requirements.txt" 2>&1 | Out-Host
$ec = $LASTEXITCODE; $ErrorActionPreference = $prev
if ($ec -ne 0) { Log-Error "pip install 失败，退出码: $ec" }
Log-OK "Python 依赖安装完成"

Log-Info "安装 Patchright Chromium（账单 PDF 下载依赖）到共享目录: $pwBrowsersDir ..."
$prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
cmd /d /c "set PLAYWRIGHT_BROWSERS_PATH=$pwBrowsersDir && `"$venvPython`" -m patchright install chromium" 2>&1 | Out-Host
$ec = $LASTEXITCODE; $ErrorActionPreference = $prev
if ($ec -ne 0) { Log-Error "patchright install chromium 失败，退出码: $ec" }
Log-OK "Patchright Chromium 已安装（PLAYWRIGHT_BROWSERS_PATH=$pwBrowsersDir）"
Test-CamDependencies -PythonExe $venvPython -BrowsersDir $pwBrowsersDir

# =====================================================================
Log-Step "Step 7: 检查系统 Google Chrome"
# =====================================================================
$chromePaths = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$chromeExe = ""
foreach ($p in $chromePaths) {
    if ($p -and (Test-Path $p)) { $chromeExe = $p; break }
}
if (-not $chromeExe) {
    $chromeCmd = Get-Command chrome.exe -ErrorAction SilentlyContinue
    if ($chromeCmd) { $chromeExe = $chromeCmd.Source }
}

if (-not $chromeExe -and -not $SkipChrome) {
    Install-Via-Winget -Id "Google.Chrome" -Name "Google Chrome" `
        -ExePath "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
    Refresh-Path
    foreach ($p in $chromePaths) {
        if ($p -and (Test-Path $p)) { $chromeExe = $p; break }
    }
}

if (-not $chromeExe) {
    Log-Error "未检测到系统 Google Chrome。Windows 必须使用系统 Chrome，不能使用 patchright 内置 Chromium。"
}
Log-OK "系统 Chrome 可用: $chromeExe"

# =====================================================================
Log-Step "Step 8: 初始化目录结构"
# =====================================================================
foreach ($dir in @("data", "data\exports", "data\exports\accounts", "data\browser_profiles")) {
    $p = "$DeployDir\$dir"
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null }
}
foreach ($dir in @("D:\cam-data", $chromeProfilesDir)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}
Log-OK "账号库目录: D:\cam-data"
Log-OK "Chrome profiles 目录: $chromeProfilesDir"
Log-OK "data 目录就绪"

# =====================================================================
Log-Step "Step 9: 创建启动脚本"
# =====================================================================
$startBat = "$DeployDir\start-web.bat"
[System.IO.File]::WriteAllLines($startBat, @(
    '@echo off',
    'chcp 65001 >nul',
    'cd /d "%~dp0"',
    'if not exist "data\logs" mkdir data\logs',
    'set PYTHONUTF8=1',
    'set PYTHONIOENCODING=utf-8',
    'set PLAYWRIGHT_BROWSERS_PATH=%~dp0data\ms-playwright',
    'rem ── 日志轮转：保留最近 3 次，每次重启从空文件开始 ──',
    'if exist "data\logs\cam.log.2" del /f /q "data\logs\cam.log.2"',
    'if exist "data\logs\cam.log.1" rename "data\logs\cam.log.1" "cam.log.2"',
    'if exist "data\logs\cam.log"   rename "data\logs\cam.log"   "cam.log.1"',
    'echo Starting Cursor Account Manager...',
    'echo Browser: visible system Chrome (HEADLESS=false)',
    'echo Web UI : http://localhost:8765',
    'echo Log    : %~dp0data\logs\cam.log',
    'set HEADLESS=false',
    '.venv\Scripts\python.exe -X utf8 -m cam web > data\logs\cam.log 2>&1'
), [System.Text.Encoding]::UTF8)
Log-OK "已创建 start-web.bat（有痕模式，双击启动，日志写入 data\logs\cam.log）"

# =====================================================================
Log-Step "Step 10: 禁用睡眠 / 休眠（服务机器必须保持在线）"
# =====================================================================
try {
    # 切换高性能电源计划（AC 电源下不睡眠）
    powercfg -setactive SCHEME_MIN 2>$null
    # 显式关闭待机和休眠超时（0 = 永不）
    powercfg -change -standby-timeout-ac  0
    powercfg -change -hibernate-timeout-ac 0
    powercfg -change -monitor-timeout-ac  0   # 显示器可以关，进程不影响
    # 彻底禁用休眠文件（释放磁盘空间，防止 hiberfil.sys 导致意外休眠）
    powercfg -hibernate off 2>$null
    Log-OK "已切换高性能计划，睡眠/休眠已禁用"
} catch {
    Log-Warn "设置电源计划失败（忽略）: $_"
}

# =====================================================================
Log-Step "Step 11: 开放防火墙端口 8765"
# =====================================================================
try {
    $ruleName = "CursorAccountManager-8765"
    if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound `
            -Protocol TCP -LocalPort 8765 -Action Allow -Profile Any | Out-Null
        Log-OK "防火墙已开放 TCP 8765"
    } else {
        Log-OK "防火墙规则已存在"
    }
} catch {
    Log-Warn "添加防火墙规则失败（需管理员权限），请手动开放 TCP 8765"
}

# =====================================================================
Log-Step "部署完成"
# =====================================================================

# 获取真实内网 IP（排除 169.254.x.x 链路本地地址和回环）
$localIP = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object {
        $_.InterfaceAlias -notlike "*Loopback*" -and
        $_.IPAddress -ne "127.0.0.1" -and
        $_.IPAddress -notlike "169.254.*"
    } |
    Select-Object -First 1).IPAddress

Write-Host ""
Write-Host "  ============================================" -ForegroundColor Green
Write-Host "  部署成功！" -ForegroundColor Green
Write-Host "  ============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  部署目录 : $DeployDir" -ForegroundColor White
Write-Host "  配置文件 : $DeployDir\.env" -ForegroundColor Yellow
Write-Host ""
  Write-Host "  启动方式：kinch 账号会话模式（Task Scheduler: CamWebService，有痕浏览器）" -ForegroundColor Cyan
Write-Host ""
Write-Host "  访问地址：" -ForegroundColor White
Write-Host "    内网   → http://${localIP}:8765" -ForegroundColor Green
Write-Host ""
Write-Host "  日志文件：$DeployDir\data\logs\cam.log" -ForegroundColor Yellow
Write-Host ""

# =====================================================================
# 启动服务（由 deploy-to-windows.sh 传 -StartServer 触发）
# =====================================================================
if ($StartServer) {
    Log-Step "启动 Web 服务（Task Scheduler）"

    $svcPython = "$DeployDir\.venv\Scripts\python.exe"
    $taskName  = "CamWebService"
    Test-CamDependencies -PythonExe $svcPython -BrowsersDir $pwBrowsersDir

    # 停止并删除旧任务 / 旧进程
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Get-WmiObject Win32_Process |
        Where-Object { $_.CommandLine -like "*-m cam web*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep 1

    # 确保日志目录存在
    $svcLogDir = "$DeployDir\data\logs"
    $svcLog    = "$svcLogDir\cam.log"
    if (-not (Test-Path $svcLogDir)) { New-Item -ItemType Directory -Path $svcLogDir -Force | Out-Null }

    # 日志轮转：保留最近 3 次，每次重启从空文件开始
    if (Test-Path "$svcLogDir\cam.log.2") { Remove-Item "$svcLogDir\cam.log.2" -Force -ErrorAction SilentlyContinue }
    if (Test-Path "$svcLogDir\cam.log.1") { Rename-Item "$svcLogDir\cam.log.1" "cam.log.2" -Force -ErrorAction SilentlyContinue }
    if (Test-Path $svcLog)                { Rename-Item $svcLog "cam.log.1" -Force -ErrorAction SilentlyContinue }
    Log-OK "日志轮转完成（旧日志 → cam.log.1 / cam.log.2）"

    # 注册任务：业务账号会话模式（有痕浏览器）
    # - 绑定当前登录账号（kinch）
    # - 触发器使用 AtLogOn（账号登录即自动拉起）
    # - 登录阶段强制有痕，便于通过 Turnstile 且可人工观察
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $cmdArg      = "/d /c ""set PYTHONUNBUFFERED=1 && set PYTHONUTF8=1 && set PLAYWRIGHT_BROWSERS_PATH=$pwBrowsersDir && set HEADLESS=false && set BROWSER_LOGIN_FORCE_HEADED=true && `"$svcPython`" -X utf8 -m cam web >> `"$svcLog`" 2>&1"""
    $action      = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmdArg -WorkingDirectory $DeployDir
    $triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    $principal   = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Highest
    $settings    = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit 0 `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -RestartCount 30 `
        -RestartInterval (New-TimeSpan -Minutes 1)   # 崩溃后 1 分钟重试，最多 30 次
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggerLogon `
        -Principal $principal -Settings $settings -Force | Out-Null
    Log-OK "任务已注册: $taskName（$currentUser + Interactive + AtLogOn + 自动重启）"

    # 启动任务
    Start-ScheduledTask -TaskName $taskName
    Start-Sleep 8

    # 确认
    $alive = Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -like "*-m cam web*" }
    if ($alive) {
        Log-OK "服务已启动 PID=$(($alive | Select-Object -First 1).ProcessId)"
    } else {
        $taskState = (Get-ScheduledTask -TaskName $taskName).State
        $taskInfo  = Get-ScheduledTaskInfo -TaskName $taskName
        $lastRun   = $taskInfo.LastTaskResult
        $lastTaskRunTime = $taskInfo.LastRunTime
        $lastTaskRunTimeStr = if ($lastTaskRunTime -and $lastTaskRunTime.Year -gt 1900) { $lastTaskRunTime.ToString("yyyy-MM-dd HH:mm:ss") } else { "N/A" }
        if ($taskState -eq "Ready" -and [int]$lastRun -eq 267011) {
            Log-Warn "任务处于 Ready 且 LastTaskResult=267011（0x41303，尚未进入该用户交互会话执行）。"
            Log-Warn "这在 kinch + Interactive 模式下是预期状态：请使用 kinch 登录桌面后自动触发，或手动执行 schtasks /run /TN $taskName"
            return
        }
        if (Test-Path $svcLog) {
            Write-Host "---- 最近日志（cam.log 尾部） ----" -ForegroundColor DarkYellow
            Get-Content $svcLog -Tail 80 | ForEach-Object { Write-Host $_ }
            Write-Host "---------------------------------" -ForegroundColor DarkYellow
        }
        Log-Error "服务启动失败，Task 状态: $taskState，LastTaskResult: $lastRun，LastRunTime: $lastTaskRunTimeStr。请检查任务配置与日志（$svcLog）"
    }
}
