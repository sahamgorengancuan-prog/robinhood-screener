@echo off
rem ===========================================================================
rem  ONE CLICK: run the whole screening pipeline once.
rem
rem  Double-click this file. It will:
rem    1. create .env from the template if missing   (safe defaults, no trading)
rem    2. build the database
rem    3. test every API and tell you what to fix
rem    4. run ONE screening cycle
rem    5. print the results
rem
rem  Optional arguments (or drag this into a cmd window):
rem    run_pipeline.bat --token 0xYourContract   screen a specific contract
rem    run_pipeline.bat --skip-checks            skip the API tests
rem    run_pipeline.bat --ui                     open the panel when finished
rem ===========================================================================

title Robinhood Chain Screener - Pipeline

call "%~dp0_env.bat"
if errorlevel 1 goto :end

"%PYTHON%" "%~dp0scripts\one_click.py" %*
if errorlevel 1 goto :failed
goto :end

:failed
echo.
echo   The pipeline exited with an error. The message above explains why.
echo   Nothing was traded: this project cannot place an order unless
echo   RUN_MODE=LIVE is set in .env, and it is ALERT_ONLY by default.
echo.

:end
echo.
pause
