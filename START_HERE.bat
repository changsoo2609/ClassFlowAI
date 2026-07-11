@echo off
setlocal
cd /d "%~dp0"
attrib +h "_runtime" >nul 2>nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0_runtime\start_classflow.ps1"
if errorlevel 1 pause
exit /b %errorlevel%
