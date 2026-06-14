@echo off
cd /d "C:\Users\brend\FeintTrade2"
if not exist logs mkdir logs
python scripts\orchestrator.py trading >> logs\trading.log 2>&1
exit /b %errorlevel%
