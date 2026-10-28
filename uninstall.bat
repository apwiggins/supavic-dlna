@echo off
setlocal EnableExtensions
title SupaVic DLNA - Uninstall
cd /d "%~dp0"

:: Undoes what start.bat set up: stops the server, removes "Start with Windows", the shortcuts and the
:: firewall rule. Your music is never touched. Optionally deletes .venv and the data/cache folder.

set "RULE=SupaVic DLNA"
echo.
echo  [1/4] Stopping SupaVic DLNA...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d='%~dp0'.ToLower(); Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -and $_.CommandLine.ToLower().Contains($d) -and ($_.CommandLine -match 'launcher\.py|main\.py|dashboard\.py') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

echo  [2/4] Removing "Start with Windows" and the shortcuts...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "SupaVic DLNA" /f >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "foreach($d in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))){ Remove-Item -LiteralPath (Join-Path $d 'SupaVic DLNA.lnk') -ErrorAction SilentlyContinue }" >nul 2>&1

echo  [3/4] Removing the firewall rule - accept the administrator prompt...
netsh advfirewall firewall show rule name="%RULE%" >nul 2>&1
if not errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Start-Process netsh -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList 'advfirewall firewall delete rule name=\"%RULE%\"' } catch { exit 1 }" >nul 2>&1
)

echo  [4/4] Optional cleanup
choice /c YN /n /m "        Delete .venv (Python packages for this app)? [Y/N] "
if not errorlevel 2 rmdir /s /q "%~dp0.venv" 2>nul
choice /c YN /n /m "        Delete settings, logs and cache (config.json and datos\)? [Y/N] "
if not errorlevel 2 (
    rmdir /s /q "%~dp0datos" 2>nul
    del /q "%~dp0config.json" 2>nul
)

echo.
echo  Done. You can now delete this folder. Your music was not touched.
echo  (A cache folder you set yourself in Settings is not deleted.)
echo.
pause
