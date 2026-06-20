@echo off
:: Weekly DEEP strategy review via headless Claude Code (opus — once/week, affordable).
:: Same prereqs as run_claude_maintenance.bat (Claude Code CLI installed + authenticated).
setlocal
cd /d "C:\Users\brend\FeintTrade2"
if not exist logs mkdir logs
set "LOG=logs\claude_weekly.log"
set "PROMPT_FILE=.claude\prompts\weekly_deep_review.md"

set "CLAUDE=%CLAUDE_BIN%"
if not defined CLAUDE (for /f "delims=" %%i in ('where claude 2^>nul') do if not defined CLAUDE set "CLAUDE=%%i")
if not defined CLAUDE if exist "%APPDATA%\npm\claude.cmd" set "CLAUDE=%APPDATA%\npm\claude.cmd"
if not defined CLAUDE if exist "%USERPROFILE%\.local\bin\claude.exe" set "CLAUDE=%USERPROFILE%\.local\bin\claude.exe"
if not defined CLAUDE (
  echo [%date% %time%] ERROR: claude CLI not found. Install ^(npm i -g @anthropic-ai/claude-code^) or set CLAUDE_BIN.>> "%LOG%"
  exit /b 1
)

echo.>> "%LOG%"
echo ===== [%date% %time%] Weekly deep review starting via "%CLAUDE%" =====>> "%LOG%"
:: Deep weekly think -> opus (override with CLAUDE_WEEKLY_MODEL). Guard hook + prompt rules
:: keep it paper-safe; commits to the current branch (never main/force).
if not defined CLAUDE_WEEKLY_MODEL set "CLAUDE_WEEKLY_MODEL=opus"
type "%PROMPT_FILE%" | "%CLAUDE%" -p --dangerously-skip-permissions --model %CLAUDE_WEEKLY_MODEL% >> "%LOG%" 2>&1
set "RC=%errorlevel%"
:: Always post the full test-suite result (per-test pass/fail) to #ft-dev-log.
python -c "import sys; sys.path.insert(0,'scripts'); import test_report; test_report.post_report(do_post=True)" >> "%LOG%" 2>&1
echo ===== [%date% %time%] Weekly deep review finished (exit %RC%) =====>> "%LOG%"
endlocal & exit /b %RC%
