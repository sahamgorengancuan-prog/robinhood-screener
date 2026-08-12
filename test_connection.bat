@echo off
rem ===========================================================================
rem  Connection and API test only - no screening, no orders.
rem
rem  Run this whenever a number looks wrong, or after editing .env. It reports
rem  which sources answer, how fast, the field names each API actually returned,
rem  and the specific fix for anything broken.
rem
rem  Optional: pass a contract address to also test ERC-20 reads, the contract
rem  scan, verification lookup and OKX price-info field mapping:
rem      test_connection.bat 0xYourContract
rem ===========================================================================

title Robinhood Chain Screener - Connection Test

call "%~dp0_env.bat"
if errorlevel 1 goto :end

if not exist "%~dp0.env" copy /y "%~dp0.env.example" "%~dp0.env" >nul

"%PYTHON%" "%~dp0scripts\probe_endpoints.py" %*

:end
echo.
pause
