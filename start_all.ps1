# ============================================================
#  Starts the whole local AI setup on THIS PC (the gateway PC):
#    1. asks for the OTHER PC's IP - just the last digits (e.g. 223)
#       or the full IP. Enter keeps the last one used.
#    2. starts the model server (2_start_server.bat, port 8080)
#    3. starts the gateway      (3_start_gateway.bat, port 8000)
#  The Cloudflare tunnel (my website\scripts\start-tunnel.ps1) is its own
#  startup task and forwards to this gateway at http://127.0.0.1:8000.
#
#  Runs at every logon once you have run 4_install_startup.bat.
#  Run by hand any time:  powershell -ExecutionPolicy Bypass -File start_all.ps1
# ============================================================
param([int]$TimeoutSeconds = 60)   # no answer this long = keep the last IP

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$ipFile = Join-Path $here 'other_pc_ip.txt'
try { $Host.UI.RawUI.WindowTitle = 'AI Server - startup' } catch {}

function Test-Port([string]$hostName, [int]$port, [int]$ms = 700) {
    $c = New-Object Net.Sockets.TcpClient
    try { return ($c.ConnectAsync($hostName, $port).Wait($ms) -and $c.Connected) } catch { return $false } finally { $c.Close() }
}

function Get-ThisIp {
    $cfg = Get-NetIPConfiguration -ErrorAction SilentlyContinue |
        Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } | Select-Object -First 1
    if ($cfg) { return @($cfg.IPv4Address)[0].IPAddress }
    return $null
}

# Read a line, but give up after $seconds if nothing was typed (returns $null).
function Read-WithTimeout([string]$prompt, [int]$seconds) {
    Write-Host -NoNewline $prompt
    try { $null = [Console]::KeyAvailable } catch { return (Read-Host) }   # no real console: plain prompt
    $buf = ''
    $deadline = (Get-Date).AddSeconds($seconds)
    while ($buf.Length -gt 0 -or (Get-Date) -lt $deadline) {
        if ([Console]::KeyAvailable) {
            $k = [Console]::ReadKey($true)
            if ($k.Key -eq 'Enter') { Write-Host ''; return $buf }
            if ($k.Key -eq 'Backspace') {
                if ($buf.Length -gt 0) { $buf = $buf.Substring(0, $buf.Length - 1); Write-Host -NoNewline "`b `b" }
            } elseif ($k.KeyChar -match '[0-9.]') {
                $buf += $k.KeyChar; Write-Host -NoNewline $k.KeyChar
            }
        } else {
            Start-Sleep -Milliseconds 100
        }
    }
    Write-Host ''
    return $null
}

# "223" -> 192.168.0.223 (same network as this PC); "1.50" -> 192.168.1.50; full IPs as they are.
function Resolve-Ip([string]$text, [string[]]$prefix) {
    $text = $text.Trim()
    if ($text -notmatch '^\d{1,3}(\.\d{1,3}){0,3}$') { return $null }
    $parts = $text.Split('.')
    $keep = 4 - $parts.Count
    $full = @()
    if ($keep -gt 0) { $full += $prefix[0..($keep - 1)] }
    $full += $parts
    foreach ($p in $full) { if ([int]$p -gt 255) { return $null } }
    if ([int]$full[3] -lt 1 -or [int]$full[3] -gt 254) { return $null }
    return (($full | ForEach-Object { [int]$_ }) -join '.')
}

# ---- 1. which IP does the other PC have today? ------------------------------
Write-Host ''
Write-Host '  ================== AI SERVER STARTUP ==================' -ForegroundColor Cyan
$thisIp = $null
for ($i = 0; $i -lt 60 -and -not $thisIp; $i++) {
    $thisIp = Get-ThisIp
    if (-not $thisIp) { if ($i -eq 0) { Write-Host '  Waiting for the network...' }; Start-Sleep -Seconds 1 }
}
$lastIp = ''
if (Test-Path $ipFile) { $lastIp = "$(Get-Content $ipFile -TotalCount 1)".Trim() }
$prefixSource = if ($thisIp) { $thisIp } elseif ($lastIp) { $lastIp } else { '192.168.0.1' }
$prefix = $prefixSource.Split('.')

Write-Host "  This PC (gateway): $thisIp"
$hint = if ($lastIp) { "Enter = keep $lastIp" } else { 'then press Enter' }
$net = ($prefix[0..2] -join '.')
while ($true) {
    $wait = if ($lastIp) { $TimeoutSeconds } else { 86400 }
    $answer = Read-WithTimeout "  OTHER PC's IP - last digits (e.g. 223 = $net.223) or full IP [$hint]: " $wait
    if ($null -eq $answer) {
        if ($lastIp) { Write-Host "  No answer in $TimeoutSeconds s - keeping $lastIp" -ForegroundColor Yellow; $otherIp = $lastIp; break }
        continue
    }
    if ($answer -eq '' -and $lastIp) { $otherIp = $lastIp; break }
    $otherIp = Resolve-Ip $answer $prefix
    if ($otherIp -and $otherIp -ne $thisIp) { break }
    Write-Host '  That is not a valid IP for the other PC - try again.' -ForegroundColor Red
}
Set-Content -Path $ipFile -Value $otherIp -Encoding ASCII

$state = 'offline'
try {
    Invoke-WebRequest "http://${otherIp}:8080/health" -UseBasicParsing -TimeoutSec 3 | Out-Null
    $state = 'online'
} catch {
    if ($_.Exception.Response) { $state = 'loading' }
}
switch ($state) {
    'online'  { Write-Host "  Other PC $otherIp : model server ONLINE" -ForegroundColor Green }
    'loading' { Write-Host "  Other PC $otherIp : model server is still loading" -ForegroundColor Yellow }
    default   { Write-Host "  Other PC $otherIp : not answering on port 8080 yet (the gateway keeps checking every 5 s)" -ForegroundColor Yellow }
}

# ---- 2. model server on this PC ----------------------------------------------
if (Test-Port '127.0.0.1' 8080) {
    Write-Host '  Model server on this PC: already running'
} else {
    Start-Process -FilePath (Join-Path $here '2_start_server.bat') -WorkingDirectory $here -WindowStyle Minimized
    Write-Host '  Model server on this PC: started (minimized window)'
}

# ---- 3. gateway ----------------------------------------------------------------
$startGateway = $true
if (Test-Port '127.0.0.1' 8000) {
    $other = $null
    try { $other = (Invoke-RestMethod 'http://127.0.0.1:8000/status' -TimeoutSec 3).backends | Where-Object { $_.name -eq 'other-pc' } } catch {}
    if ($other -and $other.url -eq "http://${otherIp}:8080") {
        Write-Host '  Gateway: already running with this IP'
        $startGateway = $false
    } else {
        $pids = @(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
        foreach ($procId in $pids) {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$procId"
            if ($proc -and $proc.CommandLine -like '*gateway.py*') {
                Stop-Process -Id $procId -Force
                Write-Host '  Gateway: restarting with the new IP'
            } else {
                Write-Host "  Port 8000 is used by another program ($($proc.Name)). Close it and run this again." -ForegroundColor Red
                Read-Host '  Press Enter to close'
                exit 1
            }
        }
        Start-Sleep -Seconds 1
    }
}
if ($startGateway) {
    $env:OTHER_PC_IP = $otherIp      # gateway.py reads this
    Start-Process -FilePath (Join-Path $here '3_start_gateway.bat') -WorkingDirectory $here -WindowStyle Minimized
    for ($i = 0; $i -lt 90 -and -not (Test-Port '127.0.0.1' 8000); $i++) { Start-Sleep -Seconds 1 }
}

# ---- summary -------------------------------------------------------------------
Write-Host ''
if (Test-Port '127.0.0.1' 8000) {
    Write-Host "  READY   Gateway: http://${thisIp}:8000/v1   Status: http://${thisIp}:8000/status" -ForegroundColor Green
} else {
    Write-Host '  The gateway did not start - open its minimized window to see why.' -ForegroundColor Red
}
Write-Host "          Other PC: $otherIp"
$tunnelLog = Join-Path $env:LOCALAPPDATA 'portfolio-tunnel\tunnel.log'
if (Test-Path $tunnelLog) {
    $last = Select-String -Path $tunnelLog -Pattern 'Tunnel up: (\S+)' | Select-Object -Last 1
    if ($last) { Write-Host "          Cloudflare tunnel: $($last.Matches[0].Groups[1].Value) -> this gateway" }
}
Write-Host ''
Write-Host '  This window closes in 30 seconds.'
Start-Sleep -Seconds 30
