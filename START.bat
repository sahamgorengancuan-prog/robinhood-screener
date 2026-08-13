@echo off
rem ===========================================================================
rem
rem   ROBINHOOD CHAIN TOKEN SCREENER  -  START HERE
rem
rem   Double-click this file. It is the only one you need.
rem
rem   It will:
rem     1. find Python and create a private virtual environment (first run only)
rem     2. install everything (first run only, a few minutes)
rem     3. create .env with safe defaults if it does not exist
rem     4. build the database
rem     5. open the control panel in your browser
rem
rem   Everything after that - setup, connection tests, running the pipeline,
rem   the kill switch - happens in the browser. No other file to run.
rem
rem   Keep this window open. Closing it stops the panel.
rem
rem ===========================================================================

title Robinhood Chain Token Screener

rem UTF-8 code page so token names and .env contents cannot break the output.
chcp 65001 >nul 2>&1

rem Always operate from this file's folder, whatever directory was double-clicked.
cd /d "%~dp0"

echo.
echo  ==========================================================================
echo    ROBINHOOD CHAIN TOKEN SCREENER
echo  ==========================================================================
echo.

set "PYTHON=%~dp0.venv\Scripts\python.exe"

if exist "%PYTHON%" goto :check_deps

rem ------------------------------------------------------------ first run
echo  [1/4] Creating the virtual environment (first run only) ...
echo.

set "PY_CMD="
where py >nul 2>&1
if not errorlevel 1 set "PY_CMD=py -3"
if not defined PY_CMD (
    where python >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python"
)
if not defined PY_CMD goto :no_python

%PY_CMD% -m venv ".venv"
if errorlevel 1 goto :venv_failed
if not exist "%PYTHON%" goto :venv_failed
goto :install

:check_deps
echo  [1/4] Checking the environment ...
"%PYTHON%" -c "import fastapi, sqlalchemy, httpx, apscheduler, pydantic_settings, gradio" >nul 2>&1
if not errorlevel 1 goto :ready

:install
echo.
echo  [2/4] Installing dependencies.
echo        The first run downloads a few hundred MB and can take several
echo        minutes. Later runs skip this step entirely.
echo.
"%PYTHON%" -m pip install --upgrade pip --quiet
"%PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 goto :pip_failed

"%PYTHON%" -c "import fastapi, sqlalchemy, httpx, apscheduler, pydantic_settings, gradio" >nul 2>&1
if errorlevel 1 goto :pip_failed
goto :ready

:ready
echo  [2/4] Dependencies OK.

rem ---------------------------------------------------------------- config
if exist "%~dp0.env" goto :have_env
echo  [3/4] Creating .env with safe defaults (ALERT_ONLY, no keys, no trading).
copy /y "%~dp0.env.example" "%~dp0.env" >nul
goto :launch

:have_env
echo  [3/4] Using the existing .env.

:launch
echo  [4/4] Starting the control panel ...
echo.
echo  ==========================================================================
echo    Panel  :  http://127.0.0.1:7860
echo    Your browser should open by itself. If not, paste that address in.
echo.
echo    Configure everything on the "Setup" tab, then use "Koneksi ^& API Test"
echo    before trusting any number the screener prints.
echo.
echo    Close this window to stop the panel.
echo  ==========================================================================
echo.

set "GRADIO_INBROWSER=true"
"%PYTHON%" -m app.ui.gradio_app
if errorlevel 1 goto :app_failed

echo.
echo  Panel stopped.
goto :end

rem ---------------------------------------------------------------- failures
:no_python
echo.
echo   [x] Python was not found on this computer.
echo.
echo   Install Python 3.11 or newer (3.14 recommended) from:
echo       https://www.python.org/downloads/
echo.
echo   IMPORTANT: during installation, tick "Add python.exe to PATH".
echo   That checkbox is the single most common cause of this error.
echo.
echo   Then double-click this file again.
echo.
goto :end

:venv_failed
echo.
echo   [x] Could not create the virtual environment in .venv
echo.
echo   Most common causes:
echo     - this folder is inside OneDrive or another synced/locked directory
echo     - antivirus blocked the file creation
echo     - the folder path contains characters Python cannot handle
echo.
echo   Try moving this folder somewhere short and local, e.g. C:\screener
echo.
goto :end

:pip_failed
echo.
echo   [x] Dependency installation failed.
echo.
echo   Check your internet connection and any corporate proxy settings.
echo   To see the full error, run this in a command prompt here:
echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
goto :end

:app_failed
echo.
echo   [x] The control panel exited with an error. The message above explains
echo       why. Nothing was traded: this project cannot place an order unless
echo       RUN_MODE=LIVE is set, and it is ALERT_ONLY by default.
echo.
echo   If .env is the problem, delete it and run this file again to regenerate
echo   it with safe defaults.
echo.
goto :end

:end
echo.
pause
