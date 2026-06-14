@echo off
cd /d C:\Users\brend\FeintTrade2

:: Window 1 — Streamlit dashboard
start "MindHub Dashboard" cmd /k "cd /d C:\Users\brend\FeintTrade2 && streamlit run dashboard.py"

:: Window 2 — Live log tail
start "MindHub Logs" cmd /k "cd /d C:\Users\brend\FeintTrade2 && title MindHub Live Logs && powershell -NoExit -Command \"Get-Content logs\crypto.log, agent.log -Wait -Tail 30 -ErrorAction SilentlyContinue\""

:: Window 3 — Run an immediate crypto cycle
start "MindHub Crypto Now" cmd /k "cd /d C:\Users\brend\FeintTrade2 && title MindHub Crypto Cycle && python scripts\orchestrator.py crypto && echo. && echo Cycle complete. This window will close in 10s. && timeout /t 10"
