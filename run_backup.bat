@echo off
cd /d "C:\Users\brend\FeintTrade2"
if not exist logs mkdir logs
python scripts\backup_state.py >> logs\backup.log 2>&1
exit /b %errorlevel%
