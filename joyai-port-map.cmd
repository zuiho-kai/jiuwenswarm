@echo off
setlocal
chcp 65001 >nul

title JoyAI port mapping

echo Mapping local JoyAI ports through wzr@47.79.124.13:31442 ...
echo   127.0.0.1:8070  -^> remote 127.0.0.1:18070  JoyAI HTTP
echo   127.0.0.1:8994  -^> remote 127.0.0.1:18994  ASR WebSocket
echo   127.0.0.1:8992  -^> remote 127.0.0.1:18992  TTS WebSocket
echo.
echo Keep this window open while using JoyAI in Jiuwen.
echo Press Ctrl+C to stop the mapping.
echo.

ssh -N ^
  -p 31442 ^
  -i "C:\Users\hiennoob\.ssh\id_ed25519_joyai" ^
  -o BatchMode=yes ^
  -o ExitOnForwardFailure=yes ^
  -o ServerAliveInterval=30 ^
  -o ServerAliveCountMax=3 ^
  -L 127.0.0.1:8070:127.0.0.1:18070 ^
  -L 127.0.0.1:8994:127.0.0.1:18994 ^
  -L 127.0.0.1:8992:127.0.0.1:18992 ^
  wzr@47.79.124.13

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo JoyAI port mapping exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
