@echo off
rem ===========================================================================
rem  Run the test suite. These are what enforce the risk gates - if they fail,
rem  do not point this at real funds.
rem ===========================================================================

title Robinhood Chain Screener - Tests

call "%~dp0_env.bat"
if errorlevel 1 goto :end

"%PYTHON%" -m pytest -q

:end
echo.
pause
