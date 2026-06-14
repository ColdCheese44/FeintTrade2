# =====================================================================
#  MindHub Trader - headless VPN tunnel as a boot-level service
#  RUN ONCE AS ADMINISTRATOR:
#     powershell -ExecutionPolicy Bypass -File register_vpn_service.ps1
#
#  Installs WireGuard (if needed) and registers a Proton WireGuard config as an
#  AUTO-START Windows service, so the VPN tunnel comes up at BOOT (before any login)
#  and stays up. The config is full-tunnel (AllowedIPs 0.0.0.0/0, ::/0), so ALL machine
#  traffic - including the headless S4U trading tasks - exits through Proton.
#
#  BEFORE RUNNING: if the ProtonVPN *app* is connected, DISCONNECT it first - two tunnels
#  to the same server conflict and the service can fail to start.
#
#  The .conf (which holds your private key) is NEVER copied into this repo. After this
#  runs, the key lives only in WireGuard's encrypted service store; delete the download.
#
#  TO UNINSTALL LATER (restores normal, non-VPN internet) - run ONLY if you want to UNDO:
#     & 'C:\Program Files\WireGuard\wireguard.exe' /uninstalltunnelservice <TunnelName>
#     (TunnelName is the .conf filename without extension, e.g. Home-US-CO-319)
# =====================================================================
param(
    [string]$ConfigPath = "C:\Users\brend\Downloads\Home-US-CO-319.conf",
    [switch]$KillSwitch   # default OFF = FAIL-OPEN (recommended for an unattended rig):
                          # full-tunnel routing while the VPN is up, but if the tunnel drops
                          # the network FALLS BACK to the normal connection instead of going
                          # dark. Pass -KillSwitch for a strict full-tunnel block (max privacy,
                          # but ALL traffic is cut if the VPN can't connect -> multi-day blackout
                          # risk while unattended).
)
$ErrorActionPreference = 'Stop'
$wg = "C:\Program Files\WireGuard\wireguard.exe"

# --- preconditions ---
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
    [Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) { throw "Run this in an ELEVATED PowerShell (Run as Administrator)." }
if (-not (Test-Path $ConfigPath)) {
    throw "WireGuard config not found: $ConfigPath  (re-export it from your Proton account -> Downloads, or pass -ConfigPath)"
}
Write-Host "NOTE: if the ProtonVPN app is currently CONNECTED, disconnect it first (two tunnels conflict)."

# --- 1. install WireGuard if missing ---
if (-not (Test-Path $wg)) {
    Write-Host "Installing WireGuard (winget)..."
    winget install --id WireGuard.WireGuard --silent --accept-source-agreements --accept-package-agreements
    Start-Sleep 3
}
if (-not (Test-Path $wg)) {
    throw "WireGuard not found at $wg after install. Install from https://www.wireguard.com/install/ and re-run."
}
Write-Host "WireGuard present at $wg"

# --- 2. (re)install the tunnel as an auto-start, boot-level service ---
$tunnel  = [IO.Path]::GetFileNameWithoutExtension($ConfigPath)
$svcName = 'WireGuardTunnel$' + $tunnel
if (Get-Service -Name $svcName -ErrorAction SilentlyContinue) {
    Write-Host "Existing tunnel service found - reinstalling to apply this config..."
    & $wg /uninstalltunnelservice $tunnel
    Start-Sleep 3
}
# Fail-open (default) vs kill-switch: WireGuard arms its "block untunneled traffic"
# kill-switch ONLY when AllowedIPs is the literal 0.0.0.0/0. Splitting it into
# 0.0.0.0/1 + 128.0.0.0/1 routes everything through the tunnel the SAME way but does NOT
# arm the kill-switch, so if the tunnel drops the PC stays online (falls back to the normal
# connection) instead of going dark. That fallback is what keeps an unattended rig trading.
$installPath = $ConfigPath
$tmpDir = $null
if (-not $KillSwitch) {
    $raw = (Get-Content -Raw $ConfigPath) -replace '0\.0\.0\.0/0', '0.0.0.0/1, 128.0.0.0/1' -replace '::/0', '::/1, 8000::/1'
    $tmpDir = Join-Path $env:TEMP ('wg_' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
    $installPath = Join-Path $tmpDir ([IO.Path]::GetFileName($ConfigPath))
    Set-Content -Path $installPath -Value $raw -Encoding ASCII
    Write-Host "FAIL-OPEN: full-tunnel while up; network STAYS ONLINE if the VPN drops (no blackout)."
} else {
    Write-Host "KILL-SWITCH: strict full-tunnel block - the network is CUT if the VPN cannot connect."
}
Write-Host "Registering boot-level tunnel service for '$tunnel'..."
& $wg /installtunnelservice $installPath
Start-Sleep 5
if ($tmpDir) { Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue }  # scrub the temp key copy
try { Set-Service -Name $svcName -StartupType Automatic -ErrorAction Stop } catch {}

# Make sure it is actually running right now (it auto-starts, but confirm for this session)
$s = Get-Service -Name $svcName -ErrorAction SilentlyContinue
if ($s -and $s.Status -ne 'Running') {
    try { Start-Service -Name $svcName -ErrorAction Stop; Start-Sleep 4 }
    catch { Write-Warning "Tunnel service did not start: $_  (disconnect the ProtonVPN app, then re-run)" }
}

# --- 3. verify ---
$s = Get-Service -Name $svcName -ErrorAction SilentlyContinue
if (-not $s) { throw "Tunnel service '$svcName' was not created." }
Write-Host ("Service: {0} | Status {1} | StartType {2}" -f $s.Name, $s.Status, $s.StartType)
Start-Sleep 3
try {
    $ip = Invoke-RestMethod https://api.ipify.org -TimeoutSec 12
    Write-Host "Public egress IP now: $ip   (should be a Proton exit IP)"
} catch {
    Write-Warning "Egress IP check failed (tunnel may still be negotiating): $_"
}

Write-Host ""
Write-Host "DONE - the Proton tunnel will auto-start at boot, before login (fully headless)."
Write-Host "Two finishing steps (both safe):"
Write-Host "  1. ProtonVPN APP -> turn OFF auto-connect / connect-on-start."
Write-Host "  2. Delete the downloaded config (its key is now in WireGuard's store):"
Write-Host "        Remove-Item '$ConfigPath' -Force"
Write-Host ""
Write-Host "Do NOT run any /uninstalltunnelservice command now - that UNDOES this setup."
Write-Host "(Uninstall instructions are in this script's header if you ever need them.)"
