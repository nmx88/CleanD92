# Fetch Windows media sessions (GSMTC). Prints KEY=value lines.
# Lists every session, picks Playing-with-title first, then any titled session.
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

function Safe([string]$s) {
    if ($null -eq $s) { return "" }
    return ($s -replace "[\r\n\|]", " ").Trim()
}

try {
    $null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
    $mgrType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]
    $propType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties]
    $mgr = Await-Op ($mgrType::RequestAsync()) $mgrType 5000
    $current = $mgr.GetCurrentSession()
    Write-Output ("CURRENT=" + $(if ($current) { Safe $current.SourceAppUserModelId } else { "" }))

    $sessions = @($mgr.GetSessions())
    Write-Output ("COUNT=" + $sessions.Count)
    if ($sessions.Count -eq 0) {
        Write-Output "STATUS=none"
        exit 0
    }

    $rows = @()
    for ($i = 0; $i -lt $sessions.Count; $i++) {
        $s = $sessions[$i]
        $props = Await-Op ($s.TryGetMediaPropertiesAsync()) $propType 4000
        $info = $s.GetPlaybackInfo()
        $status = if ($info) { [string]$info.PlaybackStatus } else { "Unknown" }
        $app = Safe $s.SourceAppUserModelId
        $title = Safe $props.Title
        $artist = Safe $props.Artist
        $album = Safe $props.AlbumTitle
        Write-Output ("SESSION=" + $app + "|" + $status + "|" + $title + "|" + $artist + "|" + $album)
        $rows += [pscustomobject]@{
            Index = $i; Session = $s; Props = $props
            App = $app; Status = $status; Title = $title
            Artist = $artist; Album = $album
        }
    }

    # Prefer Playing with a title, then any non-empty title, else current/first.
    $picked = $rows | Where-Object {
        $_.Status -match '^(Playing|Opened)$' -and $_.Title
    } | Select-Object -First 1
    if (-not $picked) {
        $picked = $rows | Where-Object { $_.Title } | Select-Object -First 1
    }
    if (-not $picked) {
        $picked = $rows[0]
    }

    Write-Output ("PICKED=" + $picked.Index)
    Write-Output ("APP=" + $picked.App)
    Write-Output ("STATUS=" + $picked.Status)
    Write-Output ("TITLE=" + $picked.Title)
    Write-Output ("ARTIST=" + $picked.Artist)
    Write-Output ("ALBUM=" + $picked.Album)
    $props = $picked.Props
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
    Write-Output ("THUMB_ERROR=" + $_.Exception.Message)
}

exit 0
