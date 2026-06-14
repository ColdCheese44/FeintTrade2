# Registers the hourly free-source market-research task (standalone).
# Run once (normal user is fine): powershell -ExecutionPolicy Bypass -File register_market_research.ps1
$ErrorActionPreference = "Stop"
$Root = "C:\Users\brend\FeintTrade2"
$Name = "Trading - Market Research"

if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    Write-Host "Removed existing '$Name'."
}

$trigger = New-ScheduledTaskTrigger -Daily -At 12:10AM
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At 12:10AM `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Hours 24)).Repetition

$action   = New-ScheduledTaskAction -Execute "$Root\run_market_research.bat" -WorkingDirectory $Root
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings `
    -Description "24/7 hourly free-source market research synthesis (continuous strategy refinement)" | Out-Null

Write-Host "Registered '$Name' — runs hourly, 24/7."
Write-Host "Run now:  Start-ScheduledTask -TaskName '$Name'"
