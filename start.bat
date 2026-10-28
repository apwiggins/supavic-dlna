@echo off
setlocal EnableExtensions EnableDelayedExpansion
title SupaVic DLNA
cd /d "%~dp0"

:: One-click setup and start (running from source code).
:: First run: finds or installs Python, creates .venv, installs the dependencies, allows the server
:: on your local network (Windows Firewall), creates shortcuts and turns on "Start with Windows".
:: Next runs: restarts the server (useful after updating the files).

set "VENV=%~dp0.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "VPYW=%VENV%\Scripts\pythonw.exe"
set "LAUNCHER=%~dp0launcher.py"
set "RULE=SupaVic DLNA"

echo.
echo  ==============================================
echo   SupaVic DLNA
echo  ==============================================
echo.

:: ---------------------------------------------------------------- 1/6
echo  [1/6] Stopping a previous instance...
call :stop_previous

:: ---------------------------------------------------------------- 2/6
echo  [2/6] Checking Python and the virtual environment...
if exist "%VPY%" (
    "%VPY%" -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo        The virtual environment is broken, recreating it.
        rmdir /s /q "%VENV%"
    )
)
if not exist "%VPY%" (
    set "PY="
    call :find_python
    if not defined PY (
        echo        Python was not found. Installing Python 3.12 with winget...
        winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
        call :find_python
    )
    if not defined PY (
        echo.
        echo  [ERROR] Python 3.9 or newer was not found and could not be installed.
        echo          Get it from https://www.python.org and tick "Add python.exe to PATH".
        pause
        exit /b 1
    )
    echo        Using !PY!
    "!PY!" -m venv "%VENV%"
    if errorlevel 1 (
        echo  [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
)
for /f "delims=" %%V in ('""%VPY%" --version 2>&1"') do echo        %%V

:: ---------------------------------------------------------------- 3/6
echo  [3/6] Checking dependencies...
set "INSTALL=1"
if exist "%VENV%\requirements.ok" (
    fc /b "%~dp0requirements.txt" "%VENV%\requirements.ok" >nul 2>&1 && set "INSTALL=0"
)
if "%INSTALL%"=="0" (
    "%VPY%" -c "import mutagen, PIL, pystray" >nul 2>&1 || set "INSTALL=1"
)
if "%INSTALL%"=="1" (
    echo        Installing mutagen, Pillow and pystray...
    "%VPY%" -m pip install --disable-pip-version-check -q --upgrade pip >nul 2>&1
    "%VPY%" -m pip install --disable-pip-version-check -q -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo  [ERROR] Could not install the dependencies. Check your internet connection.
        pause
        exit /b 1
    )
    copy /y "%~dp0requirements.txt" "%VENV%\requirements.ok" >nul
)
echo        Dependencies OK.

:: ---------------------------------------------------------------- 4/6
echo  [4/6] Checking Windows Firewall...
netsh advfirewall firewall show rule name="%RULE%" >nul 2>&1
if errorlevel 1 (
    echo        Allowing SupaVic DLNA on your local network - accept the administrator prompt...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Start-Process netsh -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList 'advfirewall firewall add rule name=\"%RULE%\" dir=in action=allow enable=yes profile=any remoteip=localsubnet program=\"%VPYW%\"' } catch { exit 1 }" >nul 2>&1
    netsh advfirewall firewall show rule name="%RULE%" >nul 2>&1
    if errorlevel 1 (
        echo        [WARNING] The rule was not created. If Windows asks, allow pythonw on private networks.
    ) else (
        echo        Rule created.
    )
) else (
    echo        Rule OK.
)

:: ---------------------------------------------------------------- 5/6
echo  [5/6] Shortcuts (Start menu and Desktop)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$w=New-Object -ComObject WScript.Shell; foreach($d in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))){ $s=$w.CreateShortcut((Join-Path $d 'SupaVic DLNA.lnk')); $s.TargetPath='%VPYW%'; $s.Arguments='\"%LAUNCHER%\"'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0assets\icon.ico,0'; $s.Description='SupaVic DLNA music server'; $s.Save() }" >nul 2>&1
if not exist "%VENV%\autostart.set" (
    "%VPY%" "%LAUNCHER%" --autostart on >nul 2>&1
    > "%VENV%\autostart.set" echo on
    echo        "Start with Windows" turned on ^(you can turn it off from the tray icon^).
)

:: ---------------------------------------------------------------- 6/6
echo  [6/6] Starting the server...
start "" "%VPYW%" "%LAUNCHER%"
set "PORT="
for /l %%i in (1,1,25) do (
    if not defined PORT (
        timeout /t 1 /nobreak >nul
        for /f "delims=" %%P in ('""%VPY%" "%LAUNCHER%" --ping 2>nul"') do set "PORT=%%P"
    )
)
if not defined PORT (
    echo.
    echo  [ERROR] The server did not respond. Last lines of the log:
    echo  ----------------------------------------------
    if exist "%~dp0datos\servidor.log" (
        powershell -NoProfile -Command "Get-Content -LiteralPath '%~dp0datos\servidor.log' -Tail 25 -Encoding UTF8"
    ) else (
        echo  ^(no log yet^)
    )
    echo  ----------------------------------------------
    pause
    exit /b 1
)

echo.
echo  SupaVic DLNA is running - look for its icon in the system tray.
echo  Control panel: http://localhost:%PORT%/
echo.
timeout /t 8
exit /b 0


:: ================================================================
:stop_previous
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d='%~dp0'.ToLower(); Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -and $_.CommandLine.ToLower().Contains($d) -and ($_.CommandLine -match 'launcher\.py|main\.py|dashboard\.py') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
if exist "%~dp0datos\servidor.pid" del "%~dp0datos\servidor.pid" >nul 2>&1
if exist "%~dp0datos\dashboard.pid" del "%~dp0datos\dashboard.pid" >nul 2>&1
exit /b 0

:find_python
for %%C in (python python3) do (
    if not defined PY (
        for /f "delims=" %%P in ('where %%C 2^>nul') do (
            if not defined PY (
                "%%P" -c "import sys, venv; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1 && set "PY=%%P"
            )
        )
    )
)
if not defined PY (
    where py >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%P"
    )
)
if not defined PY (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
)
if not defined PY (
    for /d %%D in ("%ProgramFiles%\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
)
exit /b 0
