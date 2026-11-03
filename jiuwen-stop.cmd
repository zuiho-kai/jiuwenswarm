@echo off
setlocal
cd /d "%~dp0"
set "JIUWENSWARM_DATA_DIR=%~dp0local-data"
set "JIUWENSWARM_HOME=%~dp0local-home"
set "PYTHONIOENCODING=utf-8"
"%~dp0.venv\Scripts\python.exe" -m jiuwenswarm.start_services --stop default
