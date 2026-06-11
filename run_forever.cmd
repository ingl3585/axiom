@echo off
rem Runs the Axiom pipeline in a loop so disconnects (daily maintenance break,
rem token expiry, network blips) simply start a fresh session. Each restart
rem re-authenticates, backfills any bars missed while disconnected, finalizes
rem the previous capture, and re-evaluates the edge gate.
rem
rem Stop it for real: Ctrl+C, then answer Y to "Terminate batch job".
cd /d "%~dp0"
:loop
.\.venv\Scripts\python.exe main.py
echo.
echo Pipeline exited. Restarting in 60 seconds (Ctrl+C then Y to stop)...
timeout /t 60 /nobreak >nul
goto loop
