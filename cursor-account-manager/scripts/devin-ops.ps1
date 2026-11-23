<#
.SYNOPSIS
  Devin 平台运维脚本（Windows 部署机 D:\deploy\cursor-account-manager）。

.EXAMPLE
  .\scripts\devin-ops.ps1 import D:\accounts-devin.xlsx
  .\scripts\devin-ops.ps1 status -Failed
  .\scripts\devin-ops.ps1 probe someone@corp.com -Headed
  .\scripts\devin-ops.ps1 collect
  .\scripts\devin-ops.ps1 retry-failed

命令：
  import <文件> [-Overwrite]   批量导入账号（CSV/Excel）
  status [-Failed]             账号与最近一次采集结果
  collect [-Email a,b] [-Notify]  立即采集（手动口径，默认不发飞书）
  daily                        按每日凌晨任务口径采集并发飞书
  retry-failed                 只重采最近一次失败的账号并发飞书
  probe <邮箱> [-Headed] [-Save]  单账号探针；失败落截图/页面文本
  notify-test [-Send]          播报卡片自检
  cycles [-Email a]            查看已入库的账期
  invoices [-Email a,b]        实时读取发票并直接导出 Excel（不入库）
  next-run                     下一次定时采集时间
  artifacts [N]                最近 N 个失败现场文件
  logs [N]                     服务日志里的 devin / ERROR 行
  backup-db                    备份 tokens.db
#>
param(
  [Parameter(Position = 0)][string]$Command = "help",
  [Parameter(Position = 1)][string]$Arg1 = "",
  [string[]]$Email,
  [switch]$Overwrite, [switch]$Failed, [switch]$Notify, [switch]$Headed, [switch]$Save, [switch]$Send
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONUTF8 = "1"

# 读取 .env 中少数路径类变量（业务配置由程序自己读取）
function Get-EnvValue([string]$key, [string]$default) {
  if (Test-Path "$Root\.env") {
    $m = Select-String -Path "$Root\.env" -Pattern "^$key=(.*)$" | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value.Trim('"') }
  }
  return $default
}

$Py = "$Root\.venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = (Get-Command python -ErrorAction Stop).Source }
function Cam { & $Py -X utf8 -m cam @args }

switch ($Command) {
  "import" {
    if (-not $Arg1) { throw "用法: import <文件> [-Overwrite]" }
    $a = @("devin-import", $Arg1); if ($Overwrite) { $a += "--overwrite" }; Cam @a
  }
  "status" { $a = @("devin-status"); if ($Failed) { $a += "--failed" }; Cam @a }
  "collect" {
    $a = @("devin-collect")
    foreach ($e in $Email) { $a += @("--email", $e) }
    if ($Notify) { $a += "--notify" }
    Cam @a
  }
  "daily" { Cam devin-collect --trigger scheduler }
  "retry-failed" {
    $rows = Cam devin-status --failed | Select-Object -Skip 1 | ForEach-Object { ($_ -split "\s+")[0] } | Where-Object { $_ -like "*@*" }
    if (-not $rows) { Write-Host "没有失败账号"; break }
    Write-Host "重采 $(@($rows).Count) 个失败账号…"
    $a = @("devin-collect", "--notify"); foreach ($e in $rows) { $a += @("--email", $e) }
    Cam @a
  }
  "probe" {
    if (-not $Arg1) { throw "用法: probe <邮箱> [-Headed] [-Save]" }
    $a = @("devin-probe", $Arg1); if ($Headed) { $a += "--headed" }; if ($Save) { $a += "--save" }; Cam @a
  }
  "notify-test" { $a = @("devin-notify-test"); if ($Send) { $a += "--send" }; Cam @a }
  "cycles" { $a = @("devin-cycles"); foreach ($e in $Email) { $a += @("--email", $e) }; Cam @a }
  "invoices" { $a = @("devin-invoices"); foreach ($e in $Email) { $a += @("--email", $e) }; Cam @a }
  "next-run" { Cam devin-next-run }
  "artifacts" {
    $n = if ($Arg1) { [int]$Arg1 } else { 10 }
    $dir = Join-Path $Root (Get-EnvValue "DEVIN_ARTIFACTS_DIR" "data\artifacts\devin")
    if (Test-Path $dir) { Get-ChildItem $dir | Sort-Object LastWriteTime -Descending | Select-Object -First $n Name, Length, LastWriteTime | Format-Table -AutoSize }
    else { Write-Host "目录不存在: $dir" }
  }
  "logs" {
    $n = if ($Arg1) { [int]$Arg1 } else { 100 }
    $log = "$Root\data\logs\cam.log"
    if (Test-Path $log) { Get-Content $log -Tail 2000 -Encoding UTF8 | Select-String -Pattern "devin|ERROR" -CaseSensitive:$false | Select-Object -Last $n | ForEach-Object { $_.Line } }
    else { Write-Host "未找到日志: $log" }
  }
  "backup-db" {
    $db = Get-EnvValue "TOKENS_DB" "$Root\data\tokens.db"
    $out = [IO.Path]::ChangeExtension($db, "$(Get-Date -Format yyyyMMdd-HHmmss).bak")
    $code = "import sqlite3,sys;s=sqlite3.connect(sys.argv[1]);d=sqlite3.connect(sys.argv[2]);s.backup(d);print('已备份 ->',sys.argv[2])"
    & $Py -c $code $db $out
  }
  default { Get-Help $PSCommandPath -Full | Out-String | Write-Host }
}
