@echo off
rem ============================================================
rem  Onkyo TX-NR616 panel - launcher for Windows
rem  Usage:  start_panel.bat [IP-amplitunera]
rem  Default IP: 192.168.1.34 (zmien w config.json albo podaj argument)
rem ============================================================
setlocal
cd /d "%~dp0"
set "HOST=%~1"
if "%HOST%"=="" set "HOST=192.168.1.34"
set "MEDIA=%~dp0media"
if not exist "%MEDIA%" mkdir "%MEDIA%"

where python >nul 2>nul
if errorlevel 1 (
  echo Nie znaleziono python w PATH. Zainstaluj Python 3.8+ albo popraw sciezke.
  pause
  exit /b 1
)

start "" http://127.0.0.1:8770/
python onkyo_web.py --host "%HOST%" --media "%MEDIA%" --ui-port 8770 --media-port 8765 --bind 0.0.0.0
pause
