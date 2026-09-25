# Read Windows toast notifications via UserNotificationListener.
# Needs package identity (see tools/register_identity.ps1) and user consent.
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
    return (($s -replace "[\r\n]", " ").Trim())
}

try {
    $null = [Windows.UI.Notifications.Management.UserNotificationListener, Windows.UI.Notifications.Management, ContentType = WindowsRuntime]
    $null = [Windows.UI.Notifications.NotificationKinds, Windows.UI.Notifications, ContentType = WindowsRuntime]
    $listenerType = [Windows.UI.Notifications.Management.UserNotificationListener]
    $listener = $listenerType::Current
    $accessType = [Windows.UI.Notifications.Management.UserNotificationListenerAccessStatus]
    $access = Await-Op ($listener.RequestAccessAsync()) $accessType 5000
    Write-Output ("ACCESS=" + [string]$access)
    if ([string]$access -ne "Allowed") {
        Write-Output "ERROR=notification access not allowed"
        exit 0
    }
    $kind = [Windows.UI.Notifications.NotificationKinds]::Toast
    $list = Await-Op ($listener.GetNotificationsAsync($kind)) ([System.Collections.Generic.IReadOnlyList[Windows.UI.Notifications.UserNotification]]) 8000
    Write-Output ("COUNT=" + $list.Count)
    # Emit every toast (cap high). A low cap hid Viber behind Cursor spam.
    $n = 0
    foreach ($toast in $list) {
        if ($n -ge 60) { break }
        $app = ""
        try { $app = Safe $toast.AppInfo.DisplayInfo.DisplayName } catch {}
        $aumid = ""
        try { $aumid = Safe $toast.AppInfo.AppUserModelId } catch {}
        $title = ""
        $body = ""
        try {
            $binding = $toast.Notification.Visual.GetBinding(
                [Windows.UI.Notifications.KnownNotificationBindings]::ToastGeneric)
            if ($binding) {
                $texts = $binding.GetTextElements()
                if ($texts.Count -gt 0) { $title = Safe $texts[0].Text }
                if ($texts.Count -gt 1) { $body = Safe $texts[1].Text }
            }
        } catch {}
        $id = [string]$toast.Id
        Write-Output ("TOAST=" + $id + "|" + $aumid + "|" + $app + "|" + $title + "|" + $body)
        $n++
    }
} catch {
    Write-Output ("ERROR=" + $_.Exception.Message)
    exit 1
}
exit 0
