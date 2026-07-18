@echo off
rem Double-click launcher for Windows.
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  echo Starting the trading game - your browser will open the game page.
  echo Click "Create a room" there to become the host.
  echo Keep this window open while you play. Press Ctrl+C to stop.
  echo.
  py -3 server.py --open
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    echo Starting the trading game - your browser will open the game page.
  echo Click "Create a room" there to become the host.
    echo Keep this window open while you play. Press Ctrl+C to stop.
    echo.
    python server.py --open
  ) else (
    echo This game needs Python 3, which isn't installed yet.
    echo Install it from https://www.python.org/downloads/
    echo IMPORTANT: tick "Add python.exe to PATH" during the install, then try again.
  )
)
echo.
pause
