@echo off
setlocal

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

cd /d "%REMOTE_MIC_ROOT%"

start "" "%PYTHONW%" -m ovb --settings --remote-mic-dev-session

endlocal
