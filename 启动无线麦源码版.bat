@echo off
setlocal

set "REMOTE_MIC_ROOT=%~dp0"
:: Resolve a window-less interpreter. C:\Windows\pyw.exe only
:: exists when the "py" launcher was installed for ALL users; the python.org
:: default is a per-user install (or no launcher at all), so prefer the venv.
set "PYTHONW=%REMOTE_MIC_ROOT%.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=%LOCALAPPDATA%\Programs\Python\Launcher\pyw.exe"
if not exist "%PYTHONW%" set "PYTHONW=C:\Windows\pyw.exe"
set "PYTHONPATH=%REMOTE_MIC_ROOT%src"
set "REMOTE_MIC_DEV_SESSION=1"

if not exist "%PYTHONW%" (
    echo [Remote Mic] py launcher not found:
    echo   %PYTHONW%
    echo.
    echo Create the venv and install the packages first:
    echo   py -3.14 -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r "%REMOTE_MIC_ROOT%requirements.txt"
    echo.
    pause
    exit /b 1
)

cd /d "%REMOTE_MIC_ROOT%"

start "" "%PYTHONW%" -m ovb --settings --remote-mic-dev-session

endlocal
