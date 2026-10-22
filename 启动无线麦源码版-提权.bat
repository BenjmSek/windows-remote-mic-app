:: Portable launcher: relaunch self elevated when needed, then start the app.
:: Double-clicking this file is enough - it asks for UAC by itself.
::
:: Why this indirection exists: the app injects the voice hotkey with
:: keybd_event. Windows UIPI drops synthetic input sent from a lower
:: integrity level to a higher one, so an elevated target (Marvis runs at
:: high IL) never sees the key. The app must therefore run elevated too.
@echo off
setlocal enabledelayedexpansion

set "REMOTE_MIC_ROOT=%~dp0"
set "PYTHONW=C:\Windows\pyw.exe"
set "PYTHONPATH=%REMOTE_MIC_ROOT%src"
set "REMOTE_MIC_DEV_SESSION=1"

if not exist "%PYTHONW%" (
    echo [Remote Mic] py launcher not found:
    echo   %PYTHONW%
    echo.
    echo Install packages first:
    echo   py -3.14 -m pip install -r "%REMOTE_MIC_ROOT%requirements.txt"
    echo.
    pause
    exit /b 1
)

:: Already elevated? Then just start it.
whoami /groups | findstr /c:"S-1-16-12288" >nul 2>&1
if not errorlevel 1 goto :run

:: Not elevated. Relaunch this same script through ShellExecute "runas",
:: which is what raises the UAC prompt. Passing the elevation flag stops
:: any recursion if UAC is off or the user cancels.
if /i "%~1"=="--elevated" goto :run
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process -FilePath '%~f0' -ArgumentList '--elevated' -Verb RunAs"
exit /b 0

:run
cd /d "%REMOTE_MIC_ROOT%"
start "" "%PYTHONW%" -m ovb --settings --remote-mic-dev-session

endlocal
