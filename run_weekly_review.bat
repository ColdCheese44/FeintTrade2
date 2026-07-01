@echo off
cd /d "C:\Users\brend\FeintTrade2"
if not exist logs mkdir logs
python scripts\weekly_review.py >> logs\weekly_review.log 2>&1
exit /b %errorlevel%
