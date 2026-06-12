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

"%PYTHON_EXE%" "%LAUNCHER_SCRIPT%" "%LAUNCHER_CONFIG%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo Launcher exited with code %EXIT_CODE%.
  pause
)

exit /b %EXIT_CODE%
