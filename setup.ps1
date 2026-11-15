# ============================================================
#  One-time setup for THIS PC (works on both of your PCs)
#  - Detects your GPU (NVIDIA -> CUDA build, AMD -> Vulkan build)
#  - Downloads the newest llama.cpp Windows build into C:\llama
#  - Opens ports 8080 (model server) and 8000 (gateway) on your
#    home network so other devices can connect
#  Run it via 1_setup_this_pc.bat (as administrator).
# ============================================================
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # makes downloads much faster in PowerShell 5
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$InstallDir = 'C:\llama'

# --- must be admin (for the firewall rules) ---
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host 'Right-click 1_setup_this_pc.bat and choose "Run as administrator".' -ForegroundColor Red
    exit 1
}

# --- detect GPU and which download(s) we need ---
# Each entry is one complete set of files; the first set found in a release wins.
$gpus = @((Get-CimInstance Win32_VideoController).Name)
Write-Host "GPUs found: $($gpus -join ', ')"
if ($gpus -match 'NVIDIA') {
    $backend = 'cuda'
    $choices = @(
        @('^(?!cudart).*win-cuda-12\.4-x64\.zip$', '^cudart.*win-cuda-12\.4-x64\.zip$'),
        @('^(?!cudart).*win-cuda-13\.\d+-x64\.zip$', '^cudart.*win-cuda-13\.\d+-x64\.zip$')
    )
} elseif ($gpus -match 'Radeon|AMD') {
    $backend = 'vulkan'
    $choices = @(, @('^(?!cudart).*win-vulkan-x64\.zip$'))
} else {
    throw 'No NVIDIA or AMD graphics card found.'
}
Write-Host "Using the $backend build of llama.cpp" -ForegroundColor Cyan

# --- find the newest release that actually has those Windows downloads ---
# (llama.cpp publishes both numbered builds like b10455 and versions like v0.8.0;
#  not every release has every download, so we search the recent ones.)
$releases = Invoke-RestMethod 'https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=40'
$picked = $null
foreach ($r in $releases) {
    foreach ($set in $choices) {
        $found = @(foreach ($p in $set) {
            $r.assets | Where-Object { $_.name -match $p } | Select-Object -First 1
        })
        if ($found.Count -eq @($set).Count) {
            $picked = @{ Release = $r; Assets = $found }
            break
        }
    }
    if ($picked) { break }
}

if (-not $picked) {
    Write-Host 'Could not find a matching Windows download. Files in the newest release:' -ForegroundColor Red
    $releases[0].assets.name | Where-Object { $_ -like '*win*' } | ForEach-Object { Write-Host "   $_" }
    Write-Host 'Download manually from https://github.com/ggml-org/llama.cpp/releases and extract into C:\llama'
    exit 1
}
Write-Host "Using llama.cpp release: $($picked.Release.tag_name)"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# --- a running model server locks llama-server.exe and its DLLs: stop it first ---
$wasRunning = @(Get-Process -Name 'llama-server' -ErrorAction SilentlyContinue)
if ($wasRunning) {
    Write-Host 'Stopping the running model server so it can be updated...' -ForegroundColor Yellow
    $wasRunning | Stop-Process -Force
    Start-Sleep -Seconds 2
}
# leftovers from an earlier update (files that were in use then)
Get-ChildItem $InstallDir -Filter '*.old-*' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

function Install-File([IO.FileInfo]$file) {
    $dest = Join-Path $InstallDir $file.Name
    for ($try = 1; $try -le 3; $try++) {
        try { Copy-Item -LiteralPath $file.FullName -Destination $dest -Force -ErrorAction Stop; return $true }
        catch { Start-Sleep -Seconds 2 }
    }
    # still locked: Windows lets you rename a file that is in use, so move it aside and copy again
    if (Test-Path -LiteralPath $dest) {
        try {
            Rename-Item -LiteralPath $dest -NewName ('{0}.old-{1}' -f $file.Name, (Get-Date -Format 'yyyyMMddHHmmss')) -Force -ErrorAction Stop
            Copy-Item -LiteralPath $file.FullName -Destination $dest -Force -ErrorAction Stop
            return $true
        } catch {}
    }
    return $false
}

$failed = @()
foreach ($asset in $picked.Assets) {
    $zip = Join-Path $env:TEMP $asset.name
    $tmp = Join-Path $env:TEMP ('llama_extract_' + [guid]::NewGuid())
    Write-Host "Downloading $($asset.name) ($([math]::Round($asset.size / 1MB)) MB)..."
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip
    # GitHub publishes a SHA-256 for every release file: make sure the download isn't damaged
    if ($asset.digest -and "$($asset.digest)".StartsWith('sha256:')) {
        $expected = "$($asset.digest)".Substring(7).ToLower()
        $actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $expected) {
            Remove-Item $zip -Force -ErrorAction SilentlyContinue
            throw "The download of $($asset.name) is damaged (SHA-256 mismatch). Run this again."
        }
        Write-Host '  SHA-256 verified - this is the official, undamaged file.' -ForegroundColor DarkGreen
    }
    Expand-Archive $zip -DestinationPath $tmp -Force
    # flatten so llama-server.exe and the CUDA DLLs always end up side by side in C:\llama
    foreach ($f in Get-ChildItem $tmp -Recurse -File) {
        if (-not (Install-File $f)) { $failed += $f.Name }
    }
    Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

$exe = Join-Path $InstallDir 'llama-server.exe'
$works = $false
if (Test-Path -LiteralPath $exe) {
    try { & $exe --version *> $null; $works = ($LASTEXITCODE -eq 0) } catch { $works = $false }
}
if ($failed.Count -gt 0 -or -not $works) {
    Write-Host ''
    if ($failed.Count -gt 0) {
        Write-Host "Could not write these files into ${InstallDir}:" -ForegroundColor Red
        $failed | Select-Object -Unique | ForEach-Object { Write-Host "   $_" }
    }
    if (-not $works) { Write-Host "llama-server.exe is missing or won't start." -ForegroundColor Red }
    Write-Host ''
    Write-Host 'Nothing is running it any more, so this is almost always your antivirus blocking or' -ForegroundColor Yellow
    Write-Host 'quarantining llama.cpp (a common false positive). The download itself was verified above.' -ForegroundColor Yellow
    Write-Host 'In Bitdefender: Protection > Antivirus > Open > Settings:'
    Write-Host '  - Manage quarantine: restore llama-server.exe (turn on "Create exception for the restored files")'
    Write-Host "  - or Manage exceptions > Add an exception > $InstallDir"
    Write-Host 'Then run 1_setup_this_pc.bat again.'
    exit 1
}
Write-Host "llama.cpp installed in $InstallDir and working." -ForegroundColor Green

# --- firewall: allow other devices on your home network ---
foreach ($port in 8080, 8000) {
    $name = "Local AI (port $port)"
    if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP -LocalPort $port `
            -Action Allow -Profile Private, Domain | Out-Null
        Write-Host "Opened port $port for your home network"
    }
}

# --- show result ---
Write-Host ''
Write-Host 'GPUs llama.cpp can see:' -ForegroundColor Cyan
& "$InstallDir\llama-server.exe" --list-devices
Write-Host ''
$ip = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } |
       Select-Object -First 1).IPv4Address.IPAddress
Write-Host "Done! This PC's IP address is: $ip" -ForegroundColor Green
if ($wasRunning) {
    Write-Host 'The model server was stopped for the update. Start it again with 2_start_server.bat,' -ForegroundColor Yellow
    Write-Host 'or run start_all.ps1 (it also asks for the other PC''s IP and restarts the gateway).' -ForegroundColor Yellow
} else {
    Write-Host 'Next: double-click 2_start_server.bat'
}
