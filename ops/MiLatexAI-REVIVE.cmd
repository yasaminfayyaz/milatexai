@echo off
REM Double-click to bring the MiLatexAI server back online after a kill.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0killswitch.ps1" revive
echo.
pause
