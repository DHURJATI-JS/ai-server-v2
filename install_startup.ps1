# Makes the AI server start by itself every time you log in to Windows.
#   Install:  double-click 4_install_startup.bat
#   Remove:   powershell -ExecutionPolicy Bypass -File install_startup.ps1 -Remove
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$gatewayTask = 'AI Server - gateway PC'
$serverTask = 'AI Server - model server only'
$tunnelTask = 'Portfolio AI tunnel'
$user = "$env:USERDOMAIN\$env:USERNAME"

if ($Remove) {
    foreach ($n in $gatewayTask, $serverTask) { Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue }
    Write-Host 'Removed the AI Server startup tasks.' -ForegroundColor Yellow
    exit 0
}

Write-Host ''
Write-Host 'Which PC is this?' -ForegroundColor Cyan
Write-Host '  [1] The GATEWAY PC - at every logon: asks for the other PC''s IP, starts the model server + gateway'
Write-Host '                       (and the Cloudflare tunnel points at this gateway)'
Write-Host '  [2] The OTHER PC   - at every logon: only starts its model server'
do { $choice = (Read-Host 'Type 1 or 2').Trim() } until ($choice -in @('1', '2'))

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

if ($choice -eq '1') {
    $name = $gatewayTask; $other = $serverTask
    # Visible window, because it asks you for the other PC's IP.
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $here `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$here\start_all.ps1`""
} else {
    $name = $serverTask; $other = $gatewayTask
    $bat = Join-Path $here '2_start_server.bat'
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $here `
        -Argument "-NoProfile -WindowStyle Hidden -Command `"Start-Process -FilePath '$bat' -WorkingDirectory '$here' -WindowStyle Minimized`""
}
Unregister-ScheduledTask -TaskName $other -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Done: '$name' now runs every time you log in." -ForegroundColor Green

if ($choice -eq '1') {
    # Restart the Cloudflare tunnel so it uses this PC's gateway (its internet door, http://127.0.0.1:8001).
    if (Get-ScheduledTask -TaskName $tunnelTask -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $tunnelTask -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Start-ScheduledTask -TaskName $tunnelTask
        Write-Host "Restarted '$tunnelTask' - it now forwards to this PC's gateway." -ForegroundColor Green
    } else {
        Write-Host "Cloudflare tunnel task not found - run 'my website\scripts\setup-tunnel.cmd' once." -ForegroundColor Yellow
    }
    Write-Host ''
    $now = Read-Host 'Start everything now? [Y/n]'
    if ($now -notmatch '^[nN]') { & (Join-Path $here 'start_all.ps1') }
} else {
    $now = Read-Host 'Start the model server now? [Y/n]'
    if ($now -notmatch '^[nN]') { Start-Process -FilePath (Join-Path $here '2_start_server.bat') -WorkingDirectory $here -WindowStyle Minimized }
}
