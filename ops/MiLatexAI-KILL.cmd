@echo off
REM Double-click to instantly disable the MiLatexAI server (compute -> ~$0).
REM The domain, TLS cert and ingress are preserved; use MiLatexAI-REVIVE.cmd to undo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0killswitch.ps1" kill
echo.
pause
