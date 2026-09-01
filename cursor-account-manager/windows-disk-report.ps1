# =============================================================================
# windows-disk-report.ps1
# Read-only disk usage report for CAM on Windows.
# ASCII-only source so Windows PowerShell 5.1 can parse it without UTF-8 BOM.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File windows-disk-report.ps1
#   powershell -ExecutionPolicy Bypass -File windows-disk-report.ps1 -ScanSystem
# =============================================================================

param(
    [string]$DeployDir = "C:\deploy\cursor-account-manager",
    [switch]$ScanSystem
)

$ErrorActionPreference = "SilentlyContinue"
try { chcp 437 | Out-Null } catch { }

function Format-Size {
    param([Nullable[long]]$Bytes)
    if ($null -eq $Bytes) { return "MISSING" }
    if ($Bytes -lt 0) { return "UNREADABLE" }
    if ($Bytes -ge 1GB) { return ("{0:N2} GB" -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ("{0:N1} MB" -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ("{0:N0} KB" -f ($Bytes / 1KB)) }
    return "$Bytes B"
}

function Get-DirSize {
    param([string]$Path)
    if (-not $Path) { return $null }
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $sum = [int64]0
    Get-ChildItem -LiteralPath $Path -Force -Recurse -File -ErrorAction SilentlyContinue |
        ForEach-Object { $sum += $_.Length }
    return $sum
}

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host ("========== {0} ==========" -f $Title) -ForegroundColor Magenta
}

function Write-Row {
    param([string]$Label, $Bytes, [string]$Note = "")
    $size = Format-Size $Bytes
    if ($Note) {
        Write-Host ("  {0,-56} {1,12}    {2}" -f $Label, $size, $Note)
    } else {
        Write-Host ("  {0,-56} {1,12}" -f $Label, $size)
    }
}

$started = Get-Date
$lines = New-Object System.Collections.Generic.List[string]
function Remember {
    param([string]$Text)
    $lines.Add($Text) | Out-Null
}

Write-Host ""
Write-Host "CAM disk report (READ ONLY, deletes nothing)" -ForegroundColor Cyan
Write-Host ("start : {0}" -f $started.ToString("yyyy-MM-dd HH:mm:ss"))
Write-Host ("user  : {0}" -f $env:USERNAME)
Write-Host ("home  : {0}" -f $env:USERPROFILE)
Write-Host ("deploy: {0}" -f $DeployDir)

Write-Section "C: drive"
$drive = Get-PSDrive C
$used = $drive.Used
$free = $drive.Free
$total = $used + $free
Write-Host ("  total {0}    used {1}    free {2}    {3:N1}%" -f `
    (Format-Size $total), (Format-Size $used), (Format-Size $free), (100.0 * $used / $total))
Remember ("C: total={0} used={1} free={2}" -f (Format-Size $total), (Format-Size $used), (Format-Size $free))

Write-Section "system files / recycle bin"
foreach ($sysFile in @("C:\pagefile.sys", "C:\hiberfil.sys", "C:\swapfile.sys")) {
    $item = Get-Item -LiteralPath $sysFile -Force -ErrorAction SilentlyContinue
    $bytes = if ($item) { $item.Length } else { $null }
    Write-Row $sysFile $bytes
    Remember ("{0}={1}" -f $sysFile, (Format-Size $bytes))
}

$recyclePath = 'C:\$Recycle.Bin'
$recycle = Get-DirSize $recyclePath
Write-Row $recyclePath $recycle "deleted files stay here until emptied"
Remember ("recycle={0}" -f (Format-Size $recycle))

Write-Section "CAM related paths"
$camHome = Join-Path $env:USERPROFILE ".cam"
$profiles = Join-Path $camHome "chrome-profiles"
$known = @(
    @{ Path = $camHome; Note = "browser login data, often the largest" },
    @{ Path = $profiles; Note = "one Chrome profile per account" },
    @{ Path = "C:\deploy"; Note = "deploy root" },
    @{ Path = $DeployDir; Note = "code + .venv + data" },
    @{ Path = (Join-Path $DeployDir ".venv"); Note = "Python venv, do not delete" },
    @{ Path = (Join-Path $DeployDir "data"); Note = "runtime data" },
    @{ Path = (Join-Path $DeployDir "data\exports"); Note = "invoice PDF / Excel / ZIP" },
    @{ Path = (Join-Path $DeployDir "data\ms-playwright"); Note = "Patchright Chromium, do not delete" },
    @{ Path = (Join-Path $DeployDir "data\browser_profiles"); Note = "unused leftover dir" },
    @{ Path = (Join-Path $DeployDir "data\logs"); Note = "service logs" },
    @{ Path = (Join-Path $env:USERPROFILE "AppData\Local\Temp"); Note = "user temp" },
    @{ Path = "C:\Windows\Temp"; Note = "system temp + deploy zip" },
    @{ Path = (Join-Path $env:USERPROFILE "AppData\Local\Google\Chrome"); Note = "system Chrome cache" },
    @{ Path = (Join-Path $env:USERPROFILE "AppData\Local\ms-playwright"); Note = "default Playwright browsers" },
    @{ Path = "D:\cam-data"; Note = "CAM data on D: after migrate" },
    @{ Path = "D:\cam-data\chrome-profiles"; Note = "profiles on D: after migrate" }
)

foreach ($row in $known) {
    Write-Host ("  scanning: {0}" -f $row.Path) -ForegroundColor DarkGray
    $bytes = Get-DirSize $row.Path
    Write-Row $row.Path $bytes $row.Note
    Remember ("{0}={1}" -f $row.Path, (Format-Size $bytes))
}

Write-Section "chrome-profiles per account"
if (Test-Path -LiteralPath $profiles) {
    $cacheNames = @(
        "Cache", "Code Cache", "GPUCache", "Service Worker",
        "ShaderCache", "GrShaderCache", "DawnGraphiteCache", "DawnWebGPUCache"
    )
    $accounts = @(Get-ChildItem -LiteralPath $profiles -Directory -Force -ErrorAction SilentlyContinue)
    Write-Host ("  account folders: {0}" -f $accounts.Count)
    $accountRows = @()
    $i = 0
    foreach ($acc in $accounts) {
        $i++
        if ($i % 20 -eq 0 -or $i -eq 1) {
            Write-Host ("  progress {0}/{1}: {2}" -f $i, $accounts.Count, $acc.Name) -ForegroundColor DarkGray
        }
        $totalBytes = Get-DirSize $acc.FullName
        $cacheBytes = [int64]0
        foreach ($name in $cacheNames) {
            $p = Join-Path $acc.FullName $name
            $part = Get-DirSize $p
            if ($null -ne $part) { $cacheBytes += $part }
        }
        $accountRows += [PSCustomObject]@{
            Account = $acc.Name
            Total   = [int64]$totalBytes
            Cache   = $cacheBytes
            Other   = [int64]$totalBytes - $cacheBytes
        }
    }
    $top = $accountRows | Sort-Object Total -Descending | Select-Object -First 20
    Write-Host ""
    Write-Host ("  {0,-42} {1,12} {2,12} {3,12}" -f "account", "total", "cache", "other")
    Write-Host ("  {0}" -f ("-" * 80))
    foreach ($r in $top) {
        Write-Host ("  {0,-42} {1,12} {2,12} {3,12}" -f `
            $r.Account, (Format-Size $r.Total), (Format-Size $r.Cache), (Format-Size $r.Other))
        Remember ("profile {0} total={1} cache={2} other={3}" -f `
            $r.Account, (Format-Size $r.Total), (Format-Size $r.Cache), (Format-Size $r.Other))
    }
    if ($accountRows.Count -gt 20) {
        Write-Host ("  ... {0} more accounts omitted, showing top 20" -f ($accountRows.Count - 20))
    }
    $sumTotal = ($accountRows | Measure-Object Total -Sum).Sum
    $sumCache = ($accountRows | Measure-Object Cache -Sum).Sum
    Write-Host ""
    Write-Row "ALL accounts" $sumTotal
    Write-Row "cache-like (safe to clean)" $sumCache
    Write-Row "cookies / login state (keep if possible)" ($sumTotal - $sumCache)
    Remember ("profiles total={0} cache={1} count={2}" -f (Format-Size $sumTotal), (Format-Size $sumCache), $accounts.Count)
} else {
    Write-Host "  missing (no browser login yet)"
}

Write-Section "deploy leftovers"
$leftovers = @()
$leftovers += Get-ChildItem -LiteralPath "C:\deploy" -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like ".cam_backup_*" -or $_.Name -like ".cam_extract_*" }
$leftovers += Get-ChildItem -LiteralPath "C:\Windows\Temp" -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "cam-*.zip" -or $_.Name -eq "windows-setup.ps1" }
$leftovers += Get-ChildItem -LiteralPath $env:TEMP -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "cam-*.zip" }

if (-not $leftovers) {
    Write-Host "  no .cam_backup_* / cam-*.zip leftovers"
} else {
    foreach ($item in $leftovers | Sort-Object FullName -Unique) {
        $bytes = if ($item.PSIsContainer) { Get-DirSize $item.FullName } else { $item.Length }
        Write-Row $item.FullName $bytes
        Remember ("leftover {0}={1}" -f $item.FullName, (Format-Size $bytes))
    }
}

$exportsDir = Join-Path $DeployDir "data\exports"
if (Test-Path -LiteralPath $exportsDir) {
    Write-Section "data\exports children"
    Get-ChildItem -LiteralPath $exportsDir -Force -ErrorAction SilentlyContinue | ForEach-Object {
        $bytes = if ($_.PSIsContainer) { Get-DirSize $_.FullName } else { $_.Length }
        Write-Row $_.FullName $bytes
        Remember ("export {0}={1}" -f $_.FullName, (Format-Size $bytes))
    }
}

if ($ScanSystem) {
    Write-Section "C:\ top-level folders (slow)"
    $top = Get-ChildItem -LiteralPath "C:\" -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.PSIsContainer -and $_.Name -ne '$Recycle.Bin' }
    $topRows = @()
    foreach ($dir in $top) {
        Write-Host ("  scanning: C:\{0}" -f $dir.Name) -ForegroundColor DarkGray
        $bytes = Get-DirSize $dir.FullName
        $topRows += [PSCustomObject]@{ Name = $dir.Name; Bytes = $(if ($null -eq $bytes) { [int64]-1 } else { [int64]$bytes }) }
    }
    $topRows | Sort-Object Bytes -Descending | ForEach-Object {
        Write-Row ("C:\{0}" -f $_.Name) $(if ($_.Bytes -lt 0) { $null } else { $_.Bytes })
        Remember ("C:\{0}={1}" -f $_.Name, (Format-Size $(if ($_.Bytes -lt 0) { $null } else { $_.Bytes })))
    }
} else {
    Write-Section "C:\ top-level folders"
    Write-Host "  skipped. add -ScanSystem to measure whole C:"
}

Write-Section "what you can do later (nothing deleted now)"
Write-Host "  1. large Recycle.Bin -> empty Recycle Bin, or Clear-RecycleBin -Force"
Write-Host "  2. large chrome-profiles cache -> stop CAM, delete Cache/Code Cache/GPUCache per account"
Write-Host ("     path: {0}" -f $profiles)
Write-Host "  3. need max free space and can re-login -> delete whole chrome-profiles"
Write-Host "  4. unused exports PDF/ZIP can be deleted"
Write-Host "  5. do NOT delete .venv or data\ms-playwright"
Write-Host "  6. do NOT delete pagefile.sys"

$elapsed = (Get-Date) - $started
Write-Host ""
Write-Host ("done in {0:N0}s" -f $elapsed.TotalSeconds) -ForegroundColor Green

$reportDir = if (Test-Path "C:\deploy") { "C:\deploy" } else { $env:TEMP }
$reportPath = Join-Path $reportDir ("disk-report-{0}.txt" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
@(
    "CAM disk report",
    ("time={0}" -f $started.ToString("yyyy-MM-dd HH:mm:ss")),
    ("user={0}" -f $env:USERNAME),
    ("deploy={0}" -f $DeployDir),
    ""
) + $lines | Set-Content -LiteralPath $reportPath -Encoding ASCII
Write-Host ("report saved: {0}" -f $reportPath) -ForegroundColor Yellow
Write-Host "Send that file back and I will tell you what is safe to delete."
Write-Host ""
