@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8="
set "APP_DIR=%~dp0"
set "PY_EXE="
for %%P in (
    "%APP_DIR%.venv\Scripts\python.exe"
    "%APP_DIR%venv\Scripts\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%ProgramFiles%\Python312\python.exe"
) do (
    if not defined PY_EXE if exist "%%~P" set "PY_EXE=%%~P"
)
if not defined PY_EXE set "PY_EXE=python"
"%PY_EXE%" "%APP_DIR%main.py" --cli
echo.
pause
