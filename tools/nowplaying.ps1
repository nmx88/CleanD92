# Fetch current Windows media session (GSMTC). Prints KEY=value lines.
# Stock Windows PowerShell -- keeps winsdk out of the portable exe.
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]
if (-not $asTaskGeneric) {
    Write-Output "ERROR=AsTask not found"
    exit 1
}

function Await-Op($asyncOp, [Type]$resultType, [int]$timeoutMs = 8000) {
    $asTask = $asTaskGeneric.MakeGenericMethod($resultType)
    $netTask = $asTask.Invoke($null, @($asyncOp))
    if (-not $netTask.Wait($timeoutMs)) {
        throw "timeout after ${timeoutMs}ms"
    }
    return $netTask.Result
}

try {
    $null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
    $mgrType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]
    $mgr = Await-Op ($mgrType::RequestAsync()) $mgrType 5000
    $session = $mgr.GetCurrentSession()
    if (-not $session) {
        Write-Output "STATUS=none"
        exit 0
    }
    $propType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties]
    $props = Await-Op ($session.TryGetMediaPropertiesAsync()) $propType 5000
    $info = $session.GetPlaybackInfo()
    $status = if ($info) { [string]$info.PlaybackStatus } else { "Unknown" }
    Write-Output ("STATUS=" + $status)
    Write-Output ("TITLE=" + (($props.Title) -replace "[\r\n]", " "))
    Write-Output ("ARTIST=" + (($props.Artist) -replace "[\r\n]", " "))
    Write-Output ("ALBUM=" + (($props.AlbumTitle) -replace "[\r\n]", " "))
} catch {
    Write-Output ("ERROR=" + $_.Exception.Message)
    exit 1
}

try {
    if ($props.Thumbnail) {
        $null = [Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType = WindowsRuntime]
        $stream = Await-Op ($props.Thumbnail.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType]) 3000
        try {
            $size = [int]$stream.Size
            if ($size -gt 0 -and $size -lt 4MB) {
                $reader = [Windows.Storage.Streams.DataReader]::Create($stream)
                try {
                    Await-Op ($reader.LoadAsync([uint32]$size)) ([uint32]) 3000 | Out-Null
                    $bytes = New-Object byte[] $size
                    $reader.ReadBytes($bytes)
                    Write-Output ("THUMB_B64=" + [Convert]::ToBase64String($bytes))
                } finally {
                    $reader.Dispose()
                }
            }
        } finally {
            $stream.Dispose()
        }
    }
} catch {
    # Thumbnail is optional; keep title/artist even when art fails.
    Write-Output ("THUMB_ERROR=" + $_.Exception.Message)
}

exit 0
