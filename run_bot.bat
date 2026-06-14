@echo off
cd /d "C:\Users\brend\FeintTrade2"
if not exist logs mkdir logs

:: Kill any stale bot.py processes before starting
wmic process where "name='python.exe' and CommandLine like '%%bot.py%%'" call terminate >nul 2>&1
:: Delay via ping, not timeout: `timeout` needs a console and fails headless (session 0),
:: which would make the restart loop spin with no delay at boot before the network is up.
ping -n 3 127.0.0.1 >nul

:: Auto-restart loop — bot comes back automatically on crash or disconnect
:loop
echo [%date% %time%] Bot starting... >> logs\bot.log
python bot.py >> logs\bot.log 2>&1
echo [%date% %time%] Bot exited (code %errorlevel%), restarting in 15s... >> logs\bot.log
ping -n 16 127.0.0.1 >nul
goto loop
