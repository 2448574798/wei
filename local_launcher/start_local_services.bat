@echo off
setlocal

set "LAUNCHER_DIR=%~dp0"
set "ROOT_DIR=%LAUNCHER_DIR%..\\"
set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"
set "LAUNCHER_SCRIPT=%LAUNCHER_DIR%local_launcher.py"
set "LAUNCHER_CONFIG=%LAUNCHER_DIR%launcher_config.json"
set "EXAMPLE_CONFIG=%LAUNCHER_DIR%launcher_config.example.json"
set "RUNTIME_ENV=%LAUNCHER_DIR%.env"

if not exist "%PYTHON_EXE%" (
  echo Missing Python runtime: "%PYTHON_EXE%"
  pause
  exit /b 1
)

if not exist "%LAUNCHER_CONFIG%" (
  echo Missing launcher_config.json
  echo Copying from example...
  copy "%EXAMPLE_CONFIG%" "%LAUNCHER_CONFIG%" >nul
  echo Please edit:
  echo   "%LAUNCHER_CONFIG%"
  pause
  exit /b 1
)

if not exist "%RUNTIME_ENV%" (
  echo Missing local launcher env file
  echo Please create:
  echo   "%LAUNCHER_DIR%.env"
  pause
  exit /b 1
)

powershell -NoProfile -Command "$found = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*local_launcher.py*' }; if ($found) { $found | ForEach-Object { Write-Output ('Local launcher already running: pid=' + $_.ProcessId) }; exit 2 }"
if "%ERRORLEVEL%"=="2" (
  echo Reusing the existing local launcher. Stop the running launcher first if you want a full restart.
  timeout /t 2 /nobreak >nul
  exit /b 0
)

for /f "usebackq eol=# tokens=1* delims==" %%A in ("%RUNTIME_ENV%") do (
  if not "%%~A"=="" set "%%~A=%%~B"
)

if /I not "%WEI_REDIS_TUNNEL_ENABLED%"=="false" (
  if not defined WEI_REDIS_SSH_USER set "WEI_REDIS_SSH_USER=ubuntu"
  if not defined WEI_REDIS_LOCAL_PORT set "WEI_REDIS_LOCAL_PORT=6380"
  if not defined WEI_REDIS_REMOTE_PORT set "WEI_REDIS_REMOTE_PORT=6379"

  if not defined WEI_REDIS_SSH_HOST (
    echo WEI_REDIS_SSH_HOST is required when WEI_REDIS_TUNNEL_ENABLED is not false.
    echo Set it in "%RUNTIME_ENV%" or set WEI_REDIS_TUNNEL_ENABLED=false.
    pause
    exit /b 1
  )

  powershell -NoProfile -Command "$port=[int]$env:WEI_REDIS_LOCAL_PORT; $found=Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue; if ($found) { Write-Output ('Redis SSH tunnel already appears to be listening on 127.0.0.1:' + $port); exit 0 } exit 3"
  if "%ERRORLEVEL%"=="3" (
    where ssh >nul 2>nul
    if errorlevel 1 (
      echo ssh was not found in PATH. Install OpenSSH client first.
      pause
      exit /b 1
    )

    echo Starting Redis SSH tunnel on 127.0.0.1:%WEI_REDIS_LOCAL_PORT% to %WEI_REDIS_SSH_HOST%:127.0.0.1:%WEI_REDIS_REMOTE_PORT%
    start "Wei Redis SSH Tunnel %WEI_REDIS_LOCAL_PORT%" "%ComSpec%" /k "ssh -N -L %WEI_REDIS_LOCAL_PORT%:127.0.0.1:%WEI_REDIS_REMOTE_PORT% %WEI_REDIS_SSH_USER%@%WEI_REDIS_SSH_HOST%"
  )
) else (
  echo Skipping Redis SSH tunnel because WEI_REDIS_TUNNEL_ENABLED=false
)

"%PYTHON_EXE%" "%LAUNCHER_SCRIPT%" "%LAUNCHER_CONFIG%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo Launcher exited with code %EXIT_CODE%.
  pause
)

exit /b %EXIT_CODE%
