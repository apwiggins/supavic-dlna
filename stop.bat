@echo off
title SupaVic DLNA - Stop
cd /d "%~dp0"

:: Stops the server and the dashboard started from this folder (you can also use Quit on the tray icon).

echo.
echo  Stopping SupaVic DLNA...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d='%~dp0'.ToLower(); $p = Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -and $_.CommandLine.ToLower().Contains($d) -and ($_.CommandLine -match 'launcher\.py|main\.py|dashboard\.py') }; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Write-Host '  Stopped.' } else { Write-Host '  It was not running.' }"
if exist "%~dp0datos\servidor.pid" del "%~dp0datos\servidor.pid" >nul 2>&1
if exist "%~dp0datos\dashboard.pid" del "%~dp0datos\dashboard.pid" >nul 2>&1
echo.
timeout /t 4
