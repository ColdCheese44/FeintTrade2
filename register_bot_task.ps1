# Run as Administrator once to register the Discord bot as a startup task.
$xml = @'
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>FeintTrade Discord Bot — starts at login, restarts on failure</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>brend</UserId>
    </LogonTrigger>
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
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>10</Count>
    </RestartOnFailure>
  </Settings>
  <Actions>
    <Exec>
      <Command>C:\Users\brend\FeintTrade2\run_bot.bat</Command>
      <WorkingDirectory>C:\Users\brend\FeintTrade2</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
'@
$xml | Out-File "$env:TEMP\bot_task.xml" -Encoding Unicode
schtasks /create /tn "Trading - Discord Bot" /xml "$env:TEMP\bot_task.xml" /f
Write-Host "Done. Bot will start automatically at login and restart on crash."
