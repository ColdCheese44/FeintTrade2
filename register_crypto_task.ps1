# DEPRECATED standalone helper — register_all_tasks.ps1 is the single source of truth for
# ALL scheduled tasks. This file is kept only so an older runbook referencing it doesn't
# break; it now registers the SAME 24/7 30-minute crypto schedule as register_all_tasks.ps1.
# It previously scheduled Mon-Fri ("24/5"), which would silently DOWNGRADE the live 24/7
# crypto task if re-run after register_all_tasks.ps1. Crypto trades 24/7 — the trigger
# below runs every day.
#
# COMPATIBILITY-ONLY: this helper uses an InteractiveToken XML principal, so the task runs
# ONLY while user 'brend' is interactively logged in — UNLIKE the canonical
# register_all_tasks.ps1, which registers crypto headless via S4U (runs whether or not
# you are signed in, survives reboots). Use register_all_tasks.ps1 for production; keep
# this file only for an old runbook that references it.
#
# Run this script as Administrator once to (re-)register the 30-minute crypto trading task.
Write-Host "NOTE: register_crypto_task.ps1 is deprecated/compatibility-only (InteractiveToken — needs an active login). register_all_tasks.ps1 is the canonical headless (S4U) registrar."
$xml = @'
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Crypto research and trading cycle — runs every 30 minutes, every day (24/7)</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-06-02T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
      <Repetition>
        <Interval>PT30M</Interval>
        <Duration>P1D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>brend</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions>
    <Exec>
      <Command>C:\Users\brend\FeintTrade2\run_crypto.bat</Command>
    </Exec>
  </Actions>
</Task>
'@

$xml | Out-File "$env:TEMP\crypto_task.xml" -Encoding Unicode
schtasks /create /tn "Trading - Crypto Hourly" /xml "$env:TEMP\crypto_task.xml" /f
Write-Host "Done. Task registered."
