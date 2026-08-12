@echo off
rem ===========================================================================
rem  Shared bootstrap. Not meant to be double-clicked.
rem  Callers do:   call "%~dp0_env.bat"
rem  then:         if errorlevel 1 goto :end
rem
rem  Creates .venv on first run, installs dependencies if any are missing, and
rem  exports PYTHON pointing at the virtual environment's interpreter.
rem ===========================================================================

rem UTF-8 code page so token names and .env contents cannot break output.
chcp 65001 >nul 2>&1

rem Always operate from the repository root, whatever directory was double-clicked.
cd /d "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"

if exist "%PYTHON%" goto :check_deps

echo.
echo Creating virtual environment (.venv) ...
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

:check_deps
rem Cheap import probe: far faster than running pip every launch.
"%PYTHON%" -c "import fastapi, sqlalchemy, httpx, apscheduler, pydantic_settings, gradio" >nul 2>&1
if not errorlevel 1 goto :ok

echo.
echo Installing dependencies. The first run downloads a few hundred MB
echo and can take several minutes. Later runs skip this step.
echo.
"%PYTHON%" -m pip install --upgrade pip --quiet
"%PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 goto :pip_failed

"%PYTHON%" -c "import fastapi, sqlalchemy, httpx, apscheduler, pydantic_settings, gradio" >nul 2>&1
if errorlevel 1 goto :pip_failed

:ok
exit /b 0

rem ---------------------------------------------------------------- failures
:no_python
echo.
echo   [x] Python was not found.
echo.
echo   Install Python 3.11 or newer from https://www.python.org/downloads/
echo   During installation, tick "Add python.exe to PATH".
echo   Then run this file again.
echo.
exit /b 1

:venv_failed
echo.
echo   [x] Could not create the virtual environment in .venv
echo.
echo   Most common causes:
echo     - the folder is inside OneDrive or a synced/locked directory
echo     - antivirus blocked the file creation
echo     - the path contains characters Python cannot handle
echo.
echo   Try moving this folder to something short and local, e.g. C:\screener
echo.
exit /b 1

:pip_failed
echo.
echo   [x] Dependency installation failed.
echo.
echo   Check your internet connection and any corporate proxy settings,
echo   then run setup.bat again. To see the full error, run:
echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
exit /b 1
