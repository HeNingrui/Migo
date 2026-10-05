@echo off
setlocal
cd /d "%~dp0"
if not exist "scripts\run_demo.py" (
  echo This project is not fully extracted.
  echo Close this window. Right-click the ZIP, choose Extract All,
  echo then open Start.cmd inside the extracted project folder.
  pause
  exit /b 1
)
py -3.12 -c "import sys" >nul 2>nul
if errorlevel 1 (
  python scripts\run_demo.py %*
) else (
  py -3.12 scripts\run_demo.py %*
)
if errorlevel 1 pause
endlocal
