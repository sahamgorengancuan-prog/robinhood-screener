@echo off
rem ===========================================================================
rem  EMERGENCY STOP. Double-click to halt all order execution immediately.
rem
rem  This writes the KILL_SWITCH sentinel file. It needs no Python, no venv and
rem  no running service, so it works even when everything else is broken or the
rem  API is wedged - which is exactly when you need it.
rem
rem  Screening and alerting keep running. Only execution stops.
rem  Undo with resume_trading.bat
rem ===========================================================================

cd /d "%~dp0"
rem Redirection first: "%TIME%>" would otherwise parse the trailing digit of the
rem timestamp as a stream handle (e.g. "2>") and redirect stderr instead.
>"KILL_SWITCH" echo engaged by EMERGENCY_STOP.bat at %DATE% %TIME%

if exist "KILL_SWITCH" goto :ok

echo.
echo   [x] Could not create the KILL_SWITCH sentinel file in:
echo       %~dp0
echo   Check folder permissions. Alternative stop: close the running window,
echo   or set KILL_SWITCH=true in .env.
echo.
pause
exit /b 1

:ok
echo.
echo   ####################################################################
echo   #                                                                  #
echo   #     KILL SWITCH ENGAGED - no further orders can be placed        #
echo   #                                                                  #
echo   ####################################################################
echo.
echo   A running service picks this up on its next check; no restart needed.
echo   Screening and alerts continue as normal.
echo.
echo   To resume trading later, run resume_trading.bat
echo.
pause
