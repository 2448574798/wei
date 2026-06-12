@echo off
setlocal

set "SSH_USER=%WEI_REDIS_SSH_USER%"
if "%SSH_USER%"=="" set "SSH_USER=ubuntu"

set "SSH_HOST=%WEI_REDIS_SSH_HOST%"
if "%SSH_HOST%"=="" set "SSH_HOST=43.134.7.123"

set "LOCAL_PORT=%WEI_REDIS_LOCAL_PORT%"
if "%LOCAL_PORT%"=="" set "LOCAL_PORT=6380"

set "REMOTE_PORT=%WEI_REDIS_REMOTE_PORT%"
if "%REMOTE_PORT%"=="" set "REMOTE_PORT=6379"

set "WINDOW_TITLE=Wei Redis Tunnel %LOCAL_PORT%"

for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:":%LOCAL_PORT% .*LISTENING"') do (
    echo Redis tunnel already appears to be listening on 127.0.0.1:%LOCAL_PORT%.
    echo If this is stale, close the old tunnel window and run this script again.
    exit /b 0
)

where ssh >nul 2>nul
if errorlevel 1 (
    echo ssh was not found in PATH. Install OpenSSH client first.
    exit /b 1
)

echo Starting SSH tunnel on 127.0.0.1:%LOCAL_PORT% to %SSH_HOST%:127.0.0.1:%REMOTE_PORT%
echo Keep the opened tunnel window running while you use the local server.
start "%WINDOW_TITLE%" "%ComSpec%" /k "ssh -N -L %LOCAL_PORT%:127.0.0.1:%REMOTE_PORT% %SSH_USER%@%SSH_HOST%"

endlocal
