@echo off
setlocal

rem Start from this script's folder, even when launched from elsewhere.
cd /d "%~dp0"
if errorlevel 1 (
    echo Failed to open the project folder.
    pause
    exit /b 1
)

set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo Python environment not found: "%PYTHON%"
    echo Restore the project's .venv folder before starting the service.
    pause
    exit /b 1
)

rem The copied virtual environment still has an editable-install path to the
rem former location. Child services run from the user workspace, so expose
rem this checkout explicitly to every Python process they start.
if defined PYTHONPATH (
    set "PYTHONPATH=%CD%;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%CD%"
)
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul

echo Starting JiuwenSwarm from "%CD%"...
echo Keep this window open while using the service. Press Ctrl+C to stop it.
"%PYTHON%" -m jiuwenswarm.start_services %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo JiuwenSwarm exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
