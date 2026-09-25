# Register the sparse CleanD92 identity so UserNotificationListener works.
# Requires Developer Mode: Settings -> System -> For developers -> Developer Mode
# (not System -> Advanced). Unsigned packages will not register without it.
#
# Usage: powershell -File tools\register_identity.ps1 -AppDir <folder with CleanD92.exe>

param(
    [string]$AppDir = ""
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

if (-not $AppDir) {
    $AppDir = Split-Path -Parent $PSScriptRoot
}
$AppDir = [System.IO.Path]::GetFullPath($AppDir)

# Prefer a folder that already contains CleanD92.exe (frozen, or dist/).
$candidates = @(
    $AppDir,
    (Join-Path $AppDir "dist")
)
$ext = $null
foreach ($c in $candidates) {
    if (Test-Path (Join-Path $c "CleanD92.exe")) {
        $ext = $c
        break
    }
}
if (-not $ext) {
    Write-Output "ERROR=CleanD92.exe not found under $AppDir (or dist\). Build or download the exe first."
    exit 1
}

$srcManifest = $null
foreach ($m in @(
        (Join-Path $PSScriptRoot "..\packaging\AppxManifest.xml"),
        (Join-Path $AppDir "packaging\AppxManifest.xml"),
        (Join-Path $ext "packaging\AppxManifest.xml"),
        (Join-Path $ext "AppxManifest.xml")
    )) {
    $full = [System.IO.Path]::GetFullPath($m)
    if (Test-Path $full) {
        $srcManifest = $full
        break
    }
}
if (-not $srcManifest) {
    Write-Output "ERROR=AppxManifest.xml missing"
    exit 1
}

# Sparse logos are resolved against ExternalLocation, so copy assets beside the exe.
$destManifest = Join-Path $ext "AppxManifest.xml"
$utf8 = New-Object System.Text.UTF8Encoding $false
[IO.File]::WriteAllText($destManifest, [IO.File]::ReadAllText($srcManifest), $utf8)

$logoSrc = Join-Path (Split-Path -Parent $srcManifest) "StoreLogo.png"
$logoDst = Join-Path $ext "StoreLogo.png"
if (-not (Test-Path $logoSrc)) {
    [IO.File]::WriteAllBytes($logoDst, [Convert]::FromBase64String(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
} elseif (([IO.Path]::GetFullPath($logoSrc)) -ne ([IO.Path]::GetFullPath($logoDst))) {
    Copy-Item $logoSrc $logoDst -Force
}

try {
    # Never use -ForceApplicationShutdown: it can close CleanD92 itself.
    Add-AppxPackage -Register $destManifest -ExternalLocation $ext -ErrorAction Stop
    Write-Output "STATUS=registered"
    Write-Output ("MANIFEST=" + $destManifest)
    Write-Output ("EXTERNAL=" + $ext)
    exit 0
} catch {
    $msg = $_.Exception.Message
    Write-Output ("ERROR=" + $msg)
    if ($msg -match '0x80073CFF|developer license|sideloading') {
        Write-Output "HINT=Open Settings, search for Developer Mode (System -> For developers), turn it on, then try again. System -> Advanced is a different page."
    } elseif ($msg -match '0x80080204|manifest is invalid') {
        Write-Output "HINT=The AppxManifest.xml failed schema validation. Update to the latest CleanD92 release."
    } else {
        Write-Output "HINT=Enable Developer Mode under Settings -> System -> For developers, then try again."
    }
    exit 1
}
