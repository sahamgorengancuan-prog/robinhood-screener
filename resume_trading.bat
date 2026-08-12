@echo off
rem ===========================================================================
rem  Clears the KILL_SWITCH sentinel file, allowing execution again.
rem
rem  This clears ONE of the three kill-switch triggers. If KILL_SWITCH=true is
rem  set in .env, or the switch was engaged through the control panel, those
rem  remain in force - check the panel's "Risiko ^& Order" tab afterwards.
rem ===========================================================================

title Robinhood Chain Screener - Resume

cd /d "%~dp0"

if not exist "KILL_SWITCH" (
    echo.
    echo   The KILL_SWITCH file is not present - nothing to clear.
    echo.
    pause
    exit /b 0
)

echo.
echo   This re-enables order execution.
echo.
set "ANSWER="
set /p "ANSWER=Type RESUME and press Enter to confirm: "

if /i not "%ANSWER%"=="RESUME" (
    echo.
    echo   Aborted. The kill switch is still engaged.
    echo.
    pause
    exit /b 1
)

del /f /q "KILL_SWITCH"

if exist "KILL_SWITCH" (
    echo.
    echo   [x] Could not delete the KILL_SWITCH file. Delete it by hand:
    echo       %~dp0KILL_SWITCH
    echo.
    pause
    exit /b 1
)

echo.
echo   Kill-switch file removed.
echo.
echo   Reminder: .env KILL_SWITCH=true and the panel's switch are separate
echo   triggers. Open start_ui.bat to confirm the current state.
echo.
pause
