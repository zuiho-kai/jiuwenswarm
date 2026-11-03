@echo off
setlocal
cd /d "%~dp0"
set "JIUWENSWARM_DATA_DIR=%~dp0local-data"
set "JIUWENSWARM_HOME=%~dp0local-home"
set "JIUWENSWARM_RUNTIME_SOCKET=\\.\pipe\jiuwenswarm-team-meeting-runtime"
set "PYTHONIOENCODING=utf-8"
set "JIUWENSWARM_AGENT_SERVER_PORT=21092"
set "JIUWENSWARM_WEB_PORT=22000"
set "JIUWENSWARM_GATEWAY_PORT=22001"
set "JIUWENSWARM_FRONTEND_PORT=8173"
"%~dp0.venv\Scripts\python.exe" -m jiuwenswarm.start_services %*
