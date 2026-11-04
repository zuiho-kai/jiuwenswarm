@echo off
setlocal

rem Start JiuwenSwarm from this repository's virtual environment.
set "REPO_DIR=%~dp0"
pushd "%REPO_DIR%" >nul 2>&1
if errorlevel 1 (
    echo [jiuwen-start] Failed to enter repository directory: %REPO_DIR%
    exit /b 1
)

set "PYTHON=%REPO_DIR%.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [jiuwen-start] Python was not found. Create .venv or add Python to PATH.
        popd
        exit /b 1
    )
    set "PYTHON=python"
)

echo [jiuwen-start] Starting JiuwenSwarm...
"%PYTHON%" -m jiuwenswarm.start_services %*
set "EXIT_CODE=%ERRORLEVEL%"

popd
exit /b %EXIT_CODE%
