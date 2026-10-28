@echo off
setlocal EnableExtensions EnableDelayedExpansion
title SupaVic DLNA - Build
cd /d "%~dp0.."

:: Builds the Windows executable on your PC (the same thing GitHub Actions does on each release):
::   dist\SupaVicDLNA\SupaVicDLNA.exe          program folder
::   dist\SupaVicDLNA-<ver>-portable-win64.zip  portable zip
::   dist\SupaVicDLNA-Setup-<ver>.exe           installer (only if Inno Setup 6 is installed)

set "BVENV=%CD%\build\.venv"
set "BPY=%BVENV%\Scripts\python.exe"

echo.
echo  [1/4] Build environment (build\.venv)...
if not exist "%BPY%" (
    set "PY="
    for %%C in (py python) do if not defined PY (
        where %%C >nul 2>&1 && set "PY=%%C"
    )
    if not defined PY (
        echo  [ERROR] Python 3.9+ was not found. Install it from https://www.python.org
        pause
        exit /b 1
    )
    if /i "!PY!"=="py" (py -3 -m venv "%BVENV%") else (python -m venv "%BVENV%")
)
"%BPY%" -m pip install --disable-pip-version-check -q --upgrade pip
"%BPY%" -m pip install --disable-pip-version-check -q -r requirements.txt "pyinstaller>=6.10,<7"
if errorlevel 1 (
    echo  [ERROR] Could not install the build dependencies.
    pause
    exit /b 1
)

echo  [2/4] PyInstaller...
for /f "delims=" %%V in ('"%BPY%" build\preparar.py') do set "VER=%%V"
"%BPY%" -m PyInstaller --noconfirm --clean build\SupaVicDLNA.spec --distpath dist --workpath build\tmp
if errorlevel 1 (
    echo  [ERROR] PyInstaller failed.
    pause
    exit /b 1
)

echo  [3/4] Portable zip...
if exist "dist\SupaVicDLNA-%VER%-portable-win64.zip" del "dist\SupaVicDLNA-%VER%-portable-win64.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\SupaVicDLNA' -DestinationPath 'dist\SupaVicDLNA-%VER%-portable-win64.zip'"

echo  [4/4] Installer...
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" (
    "%ISCC%" /Q "/DAppVersion=%VER%" build\installer.iss
) else (
    echo        Inno Setup 6 is not installed, skipping the installer.
    echo        Get it with:  winget install -e --id JRSoftware.InnoSetup
)

echo.
echo  Done - version %VER%. Files in dist\
dir /b dist
echo.
pause
