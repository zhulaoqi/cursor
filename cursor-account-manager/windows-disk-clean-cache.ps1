# =============================================================================
# windows-disk-clean-cache.ps1
# Clean Chrome profile caches under D:\cam-data\chrome-profiles
# Default is PREVIEW only. Pass -DoClean to actually delete.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File windows-disk-clean-cache.ps1
#   powershell -ExecutionPolicy Bypass -File windows-disk-clean-cache.ps1 -DoClean
#   powershell -ExecutionPolicy Bypass -File windows-disk-clean-cache.ps1 -DoClean -AlsoIndexedDb
# =============================================================================

param(
    [switch]$DoClean,
    [switch]$AlsoIndexedDb
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
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $null }
    $sum = [int64]0
    Get-ChildItem -LiteralPath $Path -Force -Recurse -File -ErrorAction SilentlyContinue |
        ForEach-Object { $sum += $_.Length }
    return $sum
}

$profiles = "D:\cam-data\chrome-profiles"
$cacheNames = @(
    "Cache",
    "Code Cache",
    "GPUCache",
    "ShaderCache",
    "GrShaderCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "DawnCache",
    "GraphiteDawnCache",
    "Crashpad",
    "BrowserMetrics",
    "component_crx_cache",
    "optimization_guide_hint_cache",
    "Service Worker"
)
if ($AlsoIndexedDb) {
    $cacheNames += @("IndexedDB", "File System", "Blob Storage")
}

Write-Host ""
Write-Host "CAM chrome-profiles cache cleaner" -ForegroundColor Cyan
if ($DoClean) {
    Write-Host "mode: DELETE cache folders" -ForegroundColor Yellow
} else {
    Write-Host "mode: PREVIEW only (add -DoClean to delete)" -ForegroundColor Green
}
Write-Host ("profiles: {0}" -f $profiles)
Write-Host ("targets : {0}" -f ($cacheNames -join ", "))
Write-Host ""

if (-not (Test-Path -LiteralPath $profiles)) {
    Write-Host "profiles dir missing, nothing to do"
    exit 0
}

Write-Host "========== inspect largest profile folders ==========" -ForegroundColor Magenta
$accounts = @(Get-ChildItem -LiteralPath $profiles -Directory -Force)
Write-Host ("account folders: {0}" -f $accounts.Count)

$sample = Join-Path $profiles "cursor240_blastdrama_awsapps_com"
if (-not (Test-Path -LiteralPath $sample)) {
    $sample = $accounts[0].FullName
}
Write-Host ("sample : {0}" -f $sample)
Get-ChildItem -LiteralPath $sample -Force -ErrorAction SilentlyContinue | ForEach-Object {
    $bytes = if ($_.PSIsContainer) { Get-DirSize $_.FullName } else { $_.Length }
    Write-Host ("  {0,-40} {1,12}" -f $_.Name, (Format-Size $bytes))
}
$defaultDir = Join-Path $sample "Default"
if (Test-Path -LiteralPath $defaultDir) {
    Write-Host "  ---- Default\ ----"
    Get-ChildItem -LiteralPath $defaultDir -Force -ErrorAction SilentlyContinue | ForEach-Object {
        $bytes = if ($_.PSIsContainer) { Get-DirSize $_.FullName } else { $_.Length }
        Write-Host ("  Default\{0,-32} {1,12}" -f $_.Name, (Format-Size $bytes))
    }
}

Write-Host ""
Write-Host "========== find matching cache folders ==========" -ForegroundColor Magenta
$matches = New-Object System.Collections.Generic.List[object]
$i = 0
foreach ($acc in $accounts) {
    $i++
    if ($i % 50 -eq 0 -or $i -eq 1) {
        Write-Host ("  scan {0}/{1}" -f $i, $accounts.Count) -ForegroundColor DarkGray
    }
    Get-ChildItem -LiteralPath $acc.FullName -Recurse -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $cacheNames -contains $_.Name } |
        ForEach-Object { $matches.Add($_) | Out-Null }
}

$byName = @{}
$totalMatch = [int64]0
foreach ($dir in $matches) {
    $bytes = Get-DirSize $dir.FullName
    if ($null -eq $bytes) { continue }
    $totalMatch += $bytes
    if (-not $byName.ContainsKey($dir.Name)) { $byName[$dir.Name] = [int64]0 }
    $byName[$dir.Name] += $bytes
}

Write-Host ""
Write-Host ("matched folders: {0}" -f $matches.Count)
Write-Host ("matched size   : {0}" -f (Format-Size $totalMatch))
$byName.GetEnumerator() | Sort-Object Value -Descending | ForEach-Object {
    Write-Host ("  {0,-40} {1,12}" -f $_.Key, (Format-Size $_.Value))
}

if (-not $DoClean) {
    Write-Host ""
    Write-Host "PREVIEW done. Nothing deleted." -ForegroundColor Green
    Write-Host "If matched size looks right, stop CAM/Chrome then run:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File windows-disk-clean-cache.ps1 -DoClean"
    Write-Host "If still large after that, add -AlsoIndexedDb (may need re-login for some sites)"
    exit 0
}

Write-Host ""
Write-Host "========== stop CAM / Chrome so files are not locked ==========" -ForegroundColor Magenta
try { Stop-ScheduledTask -TaskName CamWebService -ErrorAction SilentlyContinue } catch { }
Get-WmiObject Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*-m cam web*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-Process chrome, chromium -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep 2

Write-Host "========== deleting matched folders ==========" -ForegroundColor Magenta
$deleted = [int64]0
$failed = 0
$n = 0
foreach ($dir in $matches) {
    $n++
    if ($n % 100 -eq 0) {
        Write-Host ("  delete {0}/{1}" -f $n, $matches.Count) -ForegroundColor DarkGray
    }
    $bytes = Get-DirSize $dir.FullName
    try {
        Remove-Item -LiteralPath $dir.FullName -Recurse -Force -ErrorAction Stop
        if ($null -ne $bytes) { $deleted += $bytes }
    } catch {
        $failed++
    }
}

$after = Get-DirSize $profiles
Write-Host ""
Write-Host ("deleted about : {0}" -f (Format-Size $deleted))
Write-Host ("delete failed : {0}" -f $failed)
Write-Host ("profiles now  : {0}" -f (Format-Size $after))
Write-Host "Start CAM again if needed: Start-ScheduledTask -TaskName CamWebService"
Write-Host ""
