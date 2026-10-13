@echo off
if /i "%~1"=="--run" goto run
rem Keep the console open even when parsing or startup fails.
start "Clef SSH tunnel" "%ComSpec%" /d /k ""%~f0" --run"
exit /b
:run
chcp 65001 >nul
setlocal
title Clef 监工 SSH 隧道

where ssh.exe >nul 2>&1
if errorlevel 1 (
    echo 找不到 ssh.exe，请在 Windows 可选功能中安装 OpenSSH 客户端。
    pause
    exit /b 1
)

rem Exit codes: 0 = port free, 2 = healthy Clef already reachable, 3 = port occupied.
powershell.exe -NoProfile -Command "$listener = Get-NetTCPConnection -LocalPort 18093 -State Listen -ErrorAction SilentlyContinue; if (-not $listener) { exit 0 }; try { $health = Invoke-RestMethod -Uri 'http://127.0.0.1:18093/health' -TimeoutSec 5; if ($health.ready -eq $true -and $health.model -eq 'Cloudflare/clef-flash') { exit 2 } } catch {}; exit 3"
if errorlevel 3 (
    echo 本地端口 18093 已被占用，但 Clef 健康检查未通过。
    echo 请检查旧隧道或远程服务；本脚本不会停止已有进程。
    pause
    exit /b 3
)
if errorlevel 2 (
    echo Clef 已可通过本地端口 18093 访问，不需要重复建立隧道。
    pause
    exit /b 0
)
if errorlevel 1 (
    echo 无法检查本地端口，请查看 PowerShell 错误。
    pause
    exit /b 1
)

echo 正在连接 wzr@47.79.124.13:31442 ...
echo 本地接口：http://127.0.0.1:18093/v1/decisions
echo 连接成功后窗口保持等待属于正常现象，请不要关闭。
echo 关闭此窗口或按 Ctrl+C 将断开隧道。
echo 若连接失败，请检查网络、SSH 密钥及远程服务器状态。
echo.
ssh.exe -v -N -T -L 127.0.0.1:18093:127.0.0.1:18093 -p 31442 -o BatchMode=yes -o ConnectTimeout=10 -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 wzr@47.79.124.13
set "tunnel_exit_code=%errorlevel%"
echo.
echo 隧道已退出，退出码：%tunnel_exit_code%。
echo 本脚本不会自动启动远程模型服务。
pause
exit /b %tunnel_exit_code%
