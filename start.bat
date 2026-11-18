@echo off
rem Double-click to start OutreachPilot (practice website + app) and open it in your browser.
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  python start.py %*
) else (
  py -3 start.py %*
)
pause
