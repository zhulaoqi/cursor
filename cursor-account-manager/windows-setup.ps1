# =============================================================================
# windows-setup.ps1
# 在 Windows 机器上部署 cursor-account-manager
#
# 由 deploy-to-windows.sh 远程调用，也可在 Windows 本地手动运行：
#   powershell -ExecutionPolicy Bypass -File windows-setup.ps1 `
#     -PackagePath "C:\path\to\cam-xxx.zip" `
#     -DeployDir "C:\deploy\cursor-account-manager"
# =============================================================================

param(
    [string]$PackagePath = "",
    [string]$DeployDir   = "C:\deploy\cursor-account-manager",
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

# ---------- 刷新 PATH ----------
function Refresh-Path {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path","User")
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

if ($PackagePath -and (Test-Path $PackagePath)) {
    Log-Info "解压 $PackagePath → $deployParent ..."
    $backup = $null
    if (Test-Path $DeployDir) {
        Log-Info "备份旧 .env 和 data/ ..."
        $backup = "$deployParent\.cam_backup_$(Get-Date -Format yyyyMMdd_HHmmss)"
        New-Item -ItemType Directory -Path $backup -Force | Out-Null
        if (Test-Path "$DeployDir\.env") { Copy-Item "$DeployDir\.env" $backup }
        if (Test-Path "$DeployDir\data") { Copy-Item "$DeployDir\data" $backup -Recurse }
        Remove-Item $DeployDir -Recurse -Force
    }
    Expand-Archive -Path $PackagePath -DestinationPath $deployParent -Force
    $extracted   = Join-Path $deployParent "cursor-account-manager"
    $extractNorm = [System.IO.Path]::GetFullPath($extracted)
    $deployNorm  = [System.IO.Path]::GetFullPath($DeployDir)
    if ((Test-Path $extracted) -and ($extractNorm -ne $deployNorm)) {
        Rename-Item $extracted (Split-Path $deployNorm -Leaf) -Force
    }
    Log-OK "代码解压到: $DeployDir"
    if ($backup -and (Test-Path $backup)) {
        # data/ 原样还原（数据库 / token 缓存不能丢）
        if (Test-Path "$backup\data") { Copy-Item "$backup\data" $DeployDir -Recurse -Force }
        Log-OK "已还原 data/"
        # .env 走合并逻辑（见 Step 4），不直接覆盖
    }
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
#   用户密钥类配置（PROXY / CAPSOLVER_API_KEY / TWOCAPTCHA_API_KEY）→ 从旧 .env 中读取并保留
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
    "DEFAULT_IMAP_PORT"
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

$proxyLine     = if ($userVals["PROXY"]) { "PROXY=$($userVals['PROXY'])" } else { "# PROXY=http://user:pass@host:port" }
$capsolverLine = if ($userVals["CAPSOLVER_API_KEY"]) { "CAPSOLVER_API_KEY=$($userVals['CAPSOLVER_API_KEY'])" } else { "# CAPSOLVER_API_KEY=" }
$twocapLine    = if ($userVals["TWOCAPTCHA_API_KEY"]) { "TWOCAPTCHA_API_KEY=$($userVals['TWOCAPTCHA_API_KEY'])" } else { "# TWOCAPTCHA_API_KEY=" }

$envContent = @(
    "# ─── IMAP 默认配置 ───",
    "DEFAULT_IMAP_HOST=$(Get-UserVal 'DEFAULT_IMAP_HOST' 'imap.feishu.cn')",
    "DEFAULT_IMAP_PORT=$(Get-UserVal 'DEFAULT_IMAP_PORT' '993')",
    "IMAP_SEARCH_FOLDERS=$(Get-UserVal 'IMAP_SEARCH_FOLDERS' 'INBOX,Junk,Spam')",
    "",
    "# ─── 代理（保留上次部署的值，如需修改请直接编辑此文件）───",
    $proxyLine,
    "",
    "# ─── 并发控制 ───",
    "BROWSER_LOGIN_CONCURRENCY=3",
    "API_CONCURRENCY=10",
    "",
    "# ─── Turnstile 兜底求解器 ───",
    $capsolverLine,
    $twocapLine,
    "",
    "# ─── 数据路径 ───",
    "ACCOUNTS_CSV=data/accounts.csv",
    "TOKENS_DB=data/tokens.db",
    "EXPORTS_DIR=data/exports",
    "",
    "# ─── 浏览器行为 ───",
    "HEADLESS=false",
    "VERIFICATION_CODE_TIMEOUT=120"
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
Log-Info "升级 pip ..."
& $venvPython -m pip install --upgrade pip --quiet 2>&1 | Out-Host

Log-Info "安装 requirements.txt ..."
$prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
& $venvPip install -r "$DeployDir\requirements.txt" 2>&1 | Out-Host
$ec = $LASTEXITCODE; $ErrorActionPreference = $prev
if ($ec -ne 0) { Log-Error "pip install 失败，退出码: $ec" }
Log-OK "Python 依赖安装完成"

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
  Write-Host "  启动方式：有痕模式（Task Scheduler: CamWebService，需用户已登录）" -ForegroundColor Cyan
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

    # 注册任务：InteractiveToken 模式，挂到已登录用户桌面会话。
    # S4U/后台服务会话看不到 Chrome 窗口，不适合排查 Cloudflare。
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $cmdArg      = "/c set PYTHONUNBUFFERED=1& set PYTHONUTF8=1& set HEADLESS=false& `"$svcPython`" -X utf8 -m cam web > `"$svcLog`" 2>&1"
    $action      = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmdArg -WorkingDirectory $DeployDir
    $trigger     = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    $principal   = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Highest
    $settings    = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit 0 `
        -MultipleInstances IgnoreNew `
        -RestartCount 5 `
        -RestartInterval (New-TimeSpan -Minutes 1)   # 崩溃后 1 分钟内最多重启 5 次
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Log-OK "任务已注册: $taskName（有痕模式：用户登录后启动 + 崩溃自动重启 × 5）"

    # 启动任务
    Start-ScheduledTask -TaskName $taskName
    Start-Sleep 5

    # 确认
    $alive = Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -like "*-m cam web*" }
    if ($alive) {
        Log-OK "服务已启动 PID=$(($alive | Select-Object -First 1).ProcessId)"
    } else {
        $taskState = (Get-ScheduledTask -TaskName $taskName).State
        $lastRun   = (Get-ScheduledTaskInfo -TaskName $taskName).LastTaskResult
        Log-Error "服务启动失败，Task 状态: $taskState，LastTaskResult: $lastRun（0 表示成功，其他为错误码）"
    }
}
