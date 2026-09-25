# Register the sparse CleanD92 identity so UserNotificationListener works.
# Requires Developer Mode (Settings -> Privacy & security -> For developers)
# OR a trusted certificate for the package publisher.
# Run once from an elevated PowerShell next to the app, or via the UI button.

param(
    [string]$AppDir = ""
)

$ErrorActionPreference = "Stop"
if (-not $AppDir) {
    $AppDir = Split-Path -Parent $PSScriptRoot
}
$manifest = Join-Path $PSScriptRoot "..\packaging\AppxManifest.xml"
$manifest = [System.IO.Path]::GetFullPath($manifest)
if (-not (Test-Path $manifest)) {
    # Frozen: packaging/ next to the exe
    $manifest = Join-Path $AppDir "packaging\AppxManifest.xml"
}
if (-not (Test-Path $manifest)) {
    Write-Output "ERROR=manifest missing: $manifest"
    exit 1
}

# Tiny placeholder logo required by the schema when registering.
$packDir = Split-Path -Parent $manifest
$logo = Join-Path $packDir "StoreLogo.png"
if (-not (Test-Path $logo)) {
    # 1x1 PNG
    [IO.File]::WriteAllBytes($logo, [Convert]::FromBase64String(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
}

try {
    # External location = folder that holds CleanD92.exe (source or dist).
    Add-AppxPackage -Register $manifest -ExternalLocation $AppDir -ForceApplicationShutdown -ErrorAction Stop
    Write-Output "STATUS=registered"
    Write-Output ("MANIFEST=" + $manifest)
    Write-Output ("EXTERNAL=" + $AppDir)
    exit 0
} catch {
    Write-Output ("ERROR=" + $_.Exception.Message)
    Write-Output "HINT=Enable Developer Mode, then re-run this script."
    exit 1
}
