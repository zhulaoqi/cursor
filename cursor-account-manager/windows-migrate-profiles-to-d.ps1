# =============================================================================
# windows-migrate-profiles-to-d.ps1
# Copy chrome-profiles to D:\cam-data\chrome-profiles, then delete the C: copy.
# No junction. No C: leftover. Windows app reads D: only.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File windows-migrate-profiles-to-d.ps1
#   powershell -ExecutionPolicy Bypass -File windows-migrate-profiles-to-d.ps1 -DoMigrate
# =============================================================================

param(
    [string]$Source = (Join-Path $env:USERPROFILE ".cam\chrome-profiles"),
    [string]$Destination = "D:\cam-data\chrome-profiles",
    [string]$EnvFile = $(
        if (Test-Path "D:\deploy\cursor-account-manager\.env") {
            "D:\deploy\cursor-account-manager\.env"
        } else {
            "C:\deploy\cursor-account-manager\.env"
        }
    ),
    [switch]$DoMigrate
)

$ErrorActionPreference = "Stop"
try { chcp 437 | Out-Null } catch { }

function Format-Size {
    param([Nullable[long]]$Bytes)
    if ($null -eq $Bytes) { return "MISSING" }
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

function Test-IsJunction {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $item = Get-Item -LiteralPath $Path -Force
    return [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Get-DriveFree {
    param([string]$Letter)
    $d = Get-PSDrive $Letter -ErrorAction SilentlyContinue
    if (-not $d) { return $null }
    return [int64]$d.Free
}

function Remove-CamHomeOnC {
    $camHome = Join-Path $env:USERPROFILE ".cam"
    if (Test-Path -LiteralPath $Source) {
        if (Test-IsJunction $Source) {
            cmd /c "rmdir `"$Source`""
        } else {
            cmd /c "rmdir /s /q `"$Source`""
        }
    }
    if ((Test-Path -LiteralPath $camHome) -and -not (Get-ChildItem -LiteralPath $camHome -Force -ErrorAction SilentlyContinue)) {
        cmd /c "rmdir /s /q `"$camHome`""
    }
}

Write-Host ""
Write-Host "CAM migrate chrome-profiles to D: only (no junction, no C: leftover)" -ForegroundColor Cyan
Write-Host ("source : {0}" -f $Source)
Write-Host ("dest   : {0}" -f $Destination)
Write-Host ""

if (-not (Test-Path "D:\")) {
    Write-Host "D: drive not found. Stop." -ForegroundColor Red
    exit 1
}

$cFree = Get-DriveFree "C"
$dFree = Get-DriveFree "D"
Write-Host ("C: free now = {0}" -f (Format-Size $cFree))
Write-Host ("D: free now = {0}" -f (Format-Size $dFree))

$srcExists = Test-Path -LiteralPath $Source
$srcIsLink = $srcExists -and (Test-IsJunction $Source)
$srcSize = if ($srcExists -and -not $srcIsLink) { Get-DirSize $Source } else { $null }
Write-Host ("source size = {0}" -f (Format-Size $srcSize))

if ($srcExists -and -not $srcIsLink) {
    $need = [int64]($srcSize + 2GB)
    if ($dFree -lt $need) {
        Write-Host ("D: free is not enough, need about {0}" -f (Format-Size $need)) -ForegroundColor Red
        exit 1
    }
}

if (-not $DoMigrate) {
    Write-Host ""
    Write-Host "PREVIEW. Will copy to D: then delete C:\Users\...\.cam"
    Write-Host "  powershell -ExecutionPolicy Bypass -File windows-migrate-profiles-to-d.ps1 -DoMigrate"
    exit 0
}

Write-Host "========== stop CAM / Chrome ==========" -ForegroundColor Magenta
try { Stop-ScheduledTask -TaskName CamWebService -ErrorAction SilentlyContinue } catch { }
Get-WmiObject Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*-m cam web*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-Process chrome, chromium -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep 3

$parent = Split-Path $Destination -Parent
if (-not (Test-Path -LiteralPath $parent)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
}

if ($srcExists -and -not $srcIsLink) {
    Write-Host "========== copy to D: ==========" -ForegroundColor Magenta
    & robocopy $Source $Destination /E /COPY:DAT /R:2 /W:2 /NFL /NDL /NP /NJH | Out-Host
    $rc = $LASTEXITCODE
    if ($rc -gt 7) {
        Write-Host ("robocopy failed, exit={0}. C: not deleted." -f $rc) -ForegroundColor Red
        exit 1
    }
    $dstSize = Get-DirSize $Destination
    Write-Host ("dest size = {0}" -f (Format-Size $dstSize))
    if ($null -ne $srcSize -and $null -ne $dstSize -and $dstSize -lt [int64]($srcSize * 0.95)) {
        Write-Host "dest smaller than source. C: not deleted." -ForegroundColor Red
        exit 1
    }
} elseif (-not (Test-Path -LiteralPath $Destination)) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
}

Write-Host "========== delete C: .cam ==========" -ForegroundColor Magenta
Remove-CamHomeOnC
if (Test-Path -LiteralPath $Source) {
    Write-Host "failed to remove C: source:" -ForegroundColor Red
    Write-Host $Source
    exit 1
}

if (Test-Path -LiteralPath $EnvFile) {
    $text = [IO.File]::ReadAllText($EnvFile)
    $line = "CAM_CHROME_PROFILES_DIR=$Destination"
    if ($text -match "(?m)^CAM_CHROME_PROFILES_DIR=") {
        $text = [regex]::Replace($text, "(?m)^CAM_CHROME_PROFILES_DIR=.*$", $line)
    } else {
        if (-not $text.EndsWith("`r`n") -and -not $text.EndsWith("`n")) { $text += "`r`n" }
        $text += $line + "`r`n"
    }
    [IO.File]::WriteAllText($EnvFile, $text)
}

$cAfter = Get-DriveFree "C"
$dAfter = Get-DriveFree "D"
Write-Host ""
Write-Host ("C: free now = {0}" -f (Format-Size $cAfter)) -ForegroundColor Green
Write-Host ("D: free now = {0}" -f (Format-Size $dAfter))
Write-Host "profiles live only at D:\cam-data\chrome-profiles"
Write-Host "Start CAM: Start-ScheduledTask -TaskName CamWebService"
Write-Host ""
