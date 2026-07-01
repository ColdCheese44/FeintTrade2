@echo off
cd /d "C:\Users\brend\FeintTrade2"
if not exist logs mkdir logs

:: Kill the previous bot instance by its recorded PID (reliable even headless / session 0,
:: where CommandLine-based matching is blank). Falls back to the old wmic match.
if exist bot.pid (for /f %%p in (bot.pid) do taskkill /F /PID %%p >nul 2>&1)
wmic process where "name='python.exe' and CommandLine like '%%bot.py%%'" call terminate >nul 2>&1
:: Delay via ping, not timeout: `timeout` needs a console and fails headless (session 0),
:: which would make the restart loop spin with no delay at boot before the network is up.
ping -n 3 127.0.0.1 >nul

:: Auto-restart loop — bot comes back automatically on crash or disconnect
:loop
echo [%date% %time%] Bot starting... >> logs\bot.log
python -u bot.py >> logs\bot.log 2>&1
echo [%date% %time%] Bot exited (code %errorlevel%), restarting in 15s... >> logs\bot.log
ping -n 16 127.0.0.1 >nul
goto loop
