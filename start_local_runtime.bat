@echo off
setlocal

set "ROOT_DIR=%~dp0"
set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"
set "LAUNCHER_SCRIPT=%ROOT_DIR%local_launcher\local_launcher.py"
set "LAUNCHER_CONFIG=%ROOT_DIR%local_launcher\launcher_config.json"
set "EXAMPLE_CONFIG=%ROOT_DIR%local_launcher\launcher_config.example.json"

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

"%PYTHON_EXE%" "%LAUNCHER_SCRIPT%" "%LAUNCHER_CONFIG%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo Launcher exited with code %EXIT_CODE%.
  pause
)

exit /b %EXIT_CODE%
