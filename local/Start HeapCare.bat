@echo off
setlocal
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "local\scripts\start-windows.ps1"
if errorlevel 1 pause
