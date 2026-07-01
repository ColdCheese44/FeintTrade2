@echo off
cd /d C:\Users\brend\FeintTrade2

:: Window 1 — Streamlit dashboard
start "FeintTrade Dashboard Browser" /B python scripts\browser.py open http://localhost:8501 --wait-url http://localhost:8501/_stcore/health --timeout 90
start "FeintTrade Dashboard" cmd /k "cd /d C:\Users\brend\FeintTrade2 && streamlit run dashboard.py --server.headless=true --server.port=8501"

:: Window 2 — Live log tail
start "FeintTrade Logs" cmd /k "cd /d C:\Users\brend\FeintTrade2 && title FeintTrade Live Logs && powershell -NoExit -Command \"Get-Content logs\crypto.log, agent.log -Wait -Tail 30 -ErrorAction SilentlyContinue\""

:: Window 3 — Run an immediate crypto cycle
start "FeintTrade Crypto Now" cmd /k "cd /d C:\Users\brend\FeintTrade2 && title FeintTrade Crypto Cycle && python scripts\orchestrator.py crypto && echo. && echo Cycle complete. This window will close in 10s. && timeout /t 10"
