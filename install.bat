@echo off
setlocal
title Shader Mixer - install
cd /d "%~dp0"

echo.
echo   Shader Mixer
echo   ------------
echo.

rem 1. Python 3.11+ (the Microsoft Store "python" alias doesn't count until Python is really installed)
set "PY="
py -3 -c "import sys; assert sys.version_info >= (3, 11)" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; assert sys.version_info >= (3, 11)" >nul 2>nul && set "PY=python"
if not defined PY (
  echo   Python 3.11 or newer is needed and wasn't found.
  choice /c YN /m "  Install Python 3.13 now with winget"
  if errorlevel 2 (
    echo   Get it from https://www.python.org/downloads/ then run install.bat again.
    pause & exit /b 1
  )
  winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements
  echo.
  echo   Python installed. Close this window and double-click install.bat again.
  pause & exit /b 0
)

rem 2. Close the launcher so it doesn't overwrite the new profile
echo   Close the Minecraft Launcher if it's open, then
pause

rem 3. Download Fabric, mods, shaders and packs
%PY% setup.py
if errorlevel 1 (
  echo.
  echo   Setup stopped. Read the message above, fix it, and run install.bat again.
  pause & exit /b 1
)

rem 4. Desktop + Start menu shortcuts that open the app without a console window
for /f "delims=" %%i in ('%PY% -c "import sys, pathlib; print(pathlib.Path(sys.executable).with_name('pythonw.exe'))"') do set "PYW=%%i"
powershell -NoProfile -Command ^
  "$s = New-Object -ComObject WScript.Shell;" ^
  "foreach ($d in [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs')) {" ^
  "  $l = $s.CreateShortcut((Join-Path $d 'Shader Mixer.lnk'));" ^
  "  $l.TargetPath = '%PYW%'; $l.Arguments = '\"%~dp0shader-mixer.pyw\"'; $l.WorkingDirectory = '%~dp0'; $l.Save() }"

echo.
echo   All set. Open "Shader Mixer" from your desktop or Start menu.
pause
