@echo off
rem ===========================================================================
rem  FIRST-TIME SETUP. Run this once, or whenever dependencies change.
rem
rem  Creates .venv, installs everything, creates .env, builds the database,
rem  and runs the test suite so you know the install is sound before you
rem  point it at real money.
rem ===========================================================================

title Robinhood Chain Screener - Setup

call "%~dp0_env.bat"
if errorlevel 1 goto :end

echo.
echo ==========================================================================
echo   Creating configuration
echo ==========================================================================
if exist "%~dp0.env" (
    echo   .env already exists - leaving it untouched.
) else (
    copy /y "%~dp0.env.example" "%~dp0.env" >nul
    echo   .env created from .env.example
    echo   Defaults are safe: ALERT_ONLY mode, no keys, no orders possible.
)

echo.
echo ==========================================================================
echo   Building the database
echo ==========================================================================
"%PYTHON%" "%~dp0scripts\init_db.py"
if errorlevel 1 goto :end

echo.
echo ==========================================================================
echo   Running the test suite
echo ==========================================================================
"%PYTHON%" -m pytest -q
if errorlevel 1 (
    echo.
    echo   [!] Some tests failed. Do not run this against real funds until
    echo       you understand why - the tests are what enforce the risk gates.
)

echo.
echo ==========================================================================
echo   Setup complete
echo ==========================================================================
echo.
echo   Next steps:
echo     1. Open .env in Notepad and set RH_NODE_RPC_URL
echo     2. Double-click start_ui.bat and use the "Koneksi ^& API Test" tab
echo     3. Double-click run_pipeline.bat to screen once
echo.
echo   Read README.md before changing RUN_MODE away from ALERT_ONLY.
echo.

:end
pause
