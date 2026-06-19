@echo off
:: Daily autonomous maintenance via headless Claude Code.
:: Prereqs (one-time): install the Claude Code CLI and authenticate as user 'brend'
::   npm install -g @anthropic-ai/claude-code     (or the native installer)
::   claude   (sign in once so the headless run inherits the session)
:: Optional: set CLAUDE_BIN to the full path of claude.cmd/claude.exe if not on PATH.
setlocal
cd /d "C:\Users\brend\FeintTrade2"
if not exist logs mkdir logs
set "LOG=logs\claude_maintenance.log"
set "PROMPT_FILE=.claude\prompts\daily_maintenance.md"

:: Resolve the Claude Code CLI.
set "CLAUDE=%CLAUDE_BIN%"
if not defined CLAUDE (for /f "delims=" %%i in ('where claude 2^>nul') do if not defined CLAUDE set "CLAUDE=%%i")
if not defined CLAUDE if exist "%APPDATA%\npm\claude.cmd" set "CLAUDE=%APPDATA%\npm\claude.cmd"
if not defined CLAUDE if exist "%USERPROFILE%\.local\bin\claude.exe" set "CLAUDE=%USERPROFILE%\.local\bin\claude.exe"
if not defined CLAUDE (
  echo [%date% %time%] ERROR: claude CLI not found. Install ^(npm i -g @anthropic-ai/claude-code^) or set CLAUDE_BIN.>> "%LOG%"
  exit /b 1
)

echo.>> "%LOG%"
echo ===== [%date% %time%] Daily maintenance starting via "%CLAUDE%" =====>> "%LOG%"
:: -p = headless; bypass permissions so it can autofix unattended. Guardrails are the
:: prompt's HARD RULES (.claude\prompts\daily_maintenance.md) + the subagents' baked-in
:: rules + paper-only trading. It commits to the CURRENT branch (never main/force).
:: Model is overridable via CLAUDE_MODEL.
if not defined CLAUDE_MODEL set "CLAUDE_MODEL=sonnet"
type "%PROMPT_FILE%" | "%CLAUDE%" -p --dangerously-skip-permissions --model %CLAUDE_MODEL% >> "%LOG%" 2>&1
set "RC=%errorlevel%"
echo ===== [%date% %time%] Daily maintenance finished (exit %RC%) =====>> "%LOG%"
endlocal & exit /b %RC%
