@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_console_preview.ps1"
if errorlevel 1 pause
