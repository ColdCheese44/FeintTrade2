@echo off
:: NOTE: the 15-min "Intraday Cycle" task deliberately runs the FULL `cycle` routine
:: (run_cycle: fresh data + code-enforced swing exits + a model decision), NOT the lighter
:: run_intraday() path. This is intentional and is also the largest API-cost line
:: (~$37/14d). To cut cost, change the routine invoked here — not the schedule.
cd /d "C:\Users\brend\FeintTrade2"
if not exist logs mkdir logs
python scripts\orchestrator.py cycle >> logs\intraday.log 2>&1
exit /b %errorlevel%
