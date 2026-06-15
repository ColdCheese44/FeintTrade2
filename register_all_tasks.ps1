# =====================================================================
#  FeintTrade — full Task Scheduler workflow (Mountain Time)
#  RUN ONCE AS ADMINISTRATOR:  powershell -ExecutionPolicy Bypass -File register_all_tasks.ps1
#
#  Workflow (all times are the PC's local Mountain Time, MDT/MST auto):
#    07:25  Diagnostic (auto-heal)        every day
#    07:30  Market Open Summary           Mon-Fri   overnight crypto recap (REPORT 1/3)
#    07:45  Morning Research              Mon-Fri   research -> writes journal
#    08:00  Trading Session               Mon-Fri   reads research -> decides -> trades
#    07:30-14:00  Intraday Cycle /15min   Mon-Fri   fresh data -> stops/entries
#    11:30  Diagnostic (midday)           every day
#    14:15  End of Day + detailed report  Mon-Fri   (REPORT 2/3)
#    18:15  After-hours wrap + report     Mon-Fri   (REPORT 3/3)
#    hourly Crypto cycle                  every day (24/7)
#    boot   Discord bot (auto-restart)    starts at STARTUP, headless
#
#  The flow is research -> synthesis(journal) -> decisions, by design.
#
#  HEADLESS: every task runs whether or not you are signed in and survives reboots
#  (StartWhenAvailable + restart-on-failure + battery-agnostic). The script prompts once
#  for your Windows password (guaranteed network) — press Enter to use password-less S4U.
# =====================================================================

$ErrorActionPreference = "Stop"
$Root = "C:\Users\brend\FeintTrade2"
$User = $env:USERNAME

# ── Headless logon credential ────────────────────────────────────────────────
# To run "whether or not you are logged on" (headless, survives reboots) with GUARANTEED
# network access, the task store needs your Windows account password. Windows keeps it
# encrypted (admin-only); this script uses it only locally for Register-ScheduledTask and
# never writes it to disk. Press Enter to skip and fall back to S4U (no stored password —
# works for outbound HTTPS in most setups, but a few environments restrict S4U network).
$__sec = Read-Host "Windows password for '$User' (press Enter for password-less S4U)" -AsSecureString
$TaskPassword = $null
if ($__sec -and $__sec.Length -gt 0) {
    $TaskPassword = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($__sec))
    Write-Host "  -> headless via stored password (full network access)"
} else {
    Write-Host "  -> headless via S4U (no stored password)"
}

# Clean up temporary overlap so the main schedule remains the single source of truth.
if (Get-ScheduledTask -TaskName "Trading - Crypto 30min" -ErrorAction SilentlyContinue) {
    try {
        Unregister-ScheduledTask -TaskName "Trading - Crypto 30min" -Confirm:$false -ErrorAction Stop
        Write-Host "  removed temporary overlap: Trading - Crypto 30min"
    } catch {
        Write-Warning "Could not remove Trading - Crypto 30min automatically. Re-run this script as Administrator to remove the temporary overlap."
    }
}

function Register-MhTask {
    param(
        [string]$Name,
        [string]$Bat,
        [Microsoft.Management.Infrastructure.CimInstance[]]$Triggers,
        [string]$Desc,
        [int]$LimitMinutes = 12
    )
    $action = New-ScheduledTaskAction -Execute "$Root\$Bat" -WorkingDirectory $Root
    # Resilient + reboot-proof: catch up missed runs, no duplicate instances, restart on
    # failure, and ignore battery state so a crash / power blip / reboot self-heals headless.
    $common = @{ StartWhenAvailable = $true; MultipleInstances = 'IgnoreNew';
                 RestartCount = 3; RestartInterval = (New-TimeSpan -Minutes 1);
                 AllowStartIfOnBatteries = $true; DontStopIfGoingOnBatteries = $true }
    if ($LimitMinutes -le 0) {
        $settings = New-ScheduledTaskSettingsSet @common -ExecutionTimeLimit ([TimeSpan]::Zero)
    } else {
        $settings = New-ScheduledTaskSettingsSet @common -ExecutionTimeLimit (New-TimeSpan -Minutes $LimitMinutes)
    }
    if ($TaskPassword) {
        # LogonType Password — runs whether or not the user is logged on, full network access.
        Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Triggers -Settings $settings `
            -User $User -Password $TaskPassword -RunLevel Highest -Description $Desc -Force | Out-Null
    } else {
        # S4U — runs whether or not the user is logged on, with no stored password.
        $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Highest
        Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Triggers -Settings $settings `
            -Principal $principal -Description $Desc -Force | Out-Null
    }
    Write-Host ("  registered: {0}" -f $Name)
}

$weekdays = 'Monday','Tuesday','Wednesday','Thursday','Friday'

Write-Host "Registering FeintTrade tasks..."

# 07:25 daily — diagnostic auto-heal before the session
Register-MhTask "Trading - Diagnostic AM" "run_diagnostic.bat" `
    (New-ScheduledTaskTrigger -Daily -At 7:25AM) "Pre-open self-diagnostic and auto-heal"

# 11:30 daily — midday diagnostic
Register-MhTask "Trading - Diagnostic Midday" "run_diagnostic.bat" `
    (New-ScheduledTaskTrigger -Daily -At 11:30AM) "Midday self-diagnostic and auto-heal"

# 07:30 Mon-Fri — market open summary (overnight crypto recap, report 1/3)
Register-MhTask "Trading - Market Open" "run_marketopen.bat" `
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At 7:30AM) "Market open: overnight crypto recap (report 1/3)"

# 07:45 Mon-Fri — morning research
Register-MhTask "Trading - Morning Research" "run_research.bat" `
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At 7:45AM) "Morning research -> journal"

# 08:00 Mon-Fri — trading session
Register-MhTask "Trading - Session" "run_trading.bat" `
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At 8:00AM) "Trading session: synthesis -> decisions"

# 07:30-14:00 Mon-Fri every 15 min — intraday cycle
$cycle = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At 7:30AM
$cycle.Repetition = (New-ScheduledTaskTrigger -Once -At 7:30AM `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Hours 6 -Minutes 30)).Repetition
Register-MhTask "Trading - Intraday Cycle" "run_intraday.bat" $cycle "Fresh-data cycle every 15 min during the session"

# 14:15 Mon-Fri — end of day + detailed report
Register-MhTask "Trading - EOD" "run_eod.bat" `
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At 2:15PM) "End-of-day reflection + detailed Discord report"

# 18:15 Mon-Fri — after-hours wrap + report
Register-MhTask "Trading - After Hours" "run_afterhours.bat" `
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At 6:15PM) "After-hours wrap + detailed Discord report"

# every 2 hours, 24/7 — crypto cycle. Set to BI-HOURLY permanently on 2026-06-15; keep the
# interval here in sync so re-running this registrar never reverts it to hourly. (Task name
# stays "Trading - Crypto Hourly" to overwrite the existing task in place — renaming would
# orphan the live one.)
$crypto = New-ScheduledTaskTrigger -Daily -At 12:00AM
$crypto.Repetition = (New-ScheduledTaskTrigger -Once -At 12:00AM `
    -RepetitionInterval (New-TimeSpan -Hours 2) `
    -RepetitionDuration (New-TimeSpan -Hours 24)).Repetition
Register-MhTask "Trading - Crypto Hourly" "run_crypto.bat" $crypto "24/7 bi-hourly crypto scored cycle (every 2h)"

# hourly, 24/7 — free-source market research synthesis (continuous strategy refinement)
$research = New-ScheduledTaskTrigger -Daily -At 12:10AM
$research.Repetition = (New-ScheduledTaskTrigger -Once -At 12:10AM `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Hours 24)).Repetition
Register-MhTask "Trading - Market Research" "run_market_research.bat" $research "24/7 hourly free-source market research synthesis"

# at STARTUP + logon — Discord bot (its .bat auto-restarts; no execution time limit).
# AtStartup brings it up headless on boot before anyone signs in; AtLogon is a
# belt-and-suspenders second trigger (IgnoreNew prevents a duplicate instance).
Register-MhTask "Trading - Discord Bot" "run_bot.bat" `
    @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn -User $User)) `
    "Discord bot (auto-restart loop, headless at startup)" 0

Write-Host ""
Write-Host "All tasks registered. View them with:  schtasks /query /tn ""Trading - *"""
Write-Host "Remove them with:  Get-ScheduledTask -TaskName 'Trading - *' | Unregister-ScheduledTask -Confirm:`$false"
