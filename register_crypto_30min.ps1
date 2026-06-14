$ErrorActionPreference = "Stop"

$Root = "C:\Users\brend\FeintTrade2"
$User = $env:USERNAME
$Name = "Trading - Crypto 30min"

$action = New-ScheduledTaskAction -Execute "$Root\run_crypto.bat" -WorkingDirectory $Root
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest

# Start now, repeat every 30 minutes for the next 24 hours only.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration (New-TimeSpan -Hours 24)

Register-ScheduledTask `
    -TaskName $Name `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Temporary crypto cycle every 30 min for 24 hours (remove with remove_crypto_30min.ps1)" `
    -Force | Out-Null

Write-Host ""
Write-Host "Registered: $Name"
Write-Host "Interval: every 30 minutes for 24 hours, starting now"
Write-Host "Target: run_crypto.bat -> orchestrator.py crypto"
Write-Host ""
Write-Host "Verify with:"
Write-Host "schtasks /query /tn `"$Name`" /fo LIST /v"
Write-Host ""
Write-Host "Remove with:"
Write-Host "powershell -ExecutionPolicy Bypass -File C:\Users\brend\FeintTrade2\remove_crypto_30min.ps1"
