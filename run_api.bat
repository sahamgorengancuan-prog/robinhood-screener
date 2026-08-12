@echo off
rem ===========================================================================
rem  Run the screener continuously: FastAPI + the APScheduler loop.
rem
rem  This is the "leave it running" mode. It screens every INGEST_INTERVAL_S
rem  seconds (5 minutes by default) and sends alerts.
rem
rem  Dashboard : http://127.0.0.1:8000
rem  API docs  : http://127.0.0.1:8000/docs
rem
rem  Keep this window open. Closing it stops the scheduler.
rem  To stop trading without stopping the service: EMERGENCY_STOP.bat
rem ===========================================================================

title Robinhood Chain Screener - Service

call "%~dp0_env.bat"
if errorlevel 1 goto :end

if not exist "%~dp0.env" copy /y "%~dp0.env.example" "%~dp0.env" >nul

echo.
echo   Dashboard: http://127.0.0.1:8000
echo   Stop with Ctrl+C, or close this window.
echo   Emergency stop without closing: double-click EMERGENCY_STOP.bat
echo.

"%PYTHON%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

:end
echo.
pause
