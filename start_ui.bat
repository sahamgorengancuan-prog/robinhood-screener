@echo off
rem ===========================================================================
rem  ONE CLICK: open the control panel in your browser.
rem
rem  Six tabs: connection tests, screener results, token inspector,
rem  threshold lab, risk/kill switch, and configuration.
rem
rem  The panel listens on 127.0.0.1 only and has NO password. Do not expose it.
rem  Close this window to stop it.
rem ===========================================================================

title Robinhood Chain Screener - Control Panel

call "%~dp0_env.bat"
if errorlevel 1 goto :end

if not exist "%~dp0.env" (
    echo Creating .env from the template ...
    copy /y "%~dp0.env.example" "%~dp0.env" >nul
)

rem Opens the default browser once Gradio is listening.
set "GRADIO_INBROWSER=true"

echo.
echo   Starting the control panel on http://127.0.0.1:7860
echo   Your browser should open automatically.
echo   Keep this window open. Press Ctrl+C or close it to stop.
echo.

"%PYTHON%" "%~dp0scripts\run_ui.py"

:end
echo.
pause
