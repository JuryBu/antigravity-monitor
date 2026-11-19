@echo off
setlocal
set "PYTHONUTF8="
set "PYTHONIOENCODING=utf-8"
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
    "%ProgramFiles%\Python314\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles%\Python311\python.exe"
    "%ProgramFiles%\Python310\python.exe"
) do (
    if not defined PY_EXE if exist "%%~P" set "PY_EXE=%%~P"
)
if not defined PY_EXE (
    for %%V in (3.14 3.13 3.12 3.11 3.10) do (
        if not defined PY_EXE (
            for /f "usebackq delims=" %%E in (`py -%%V -c "import sys; print(sys.executable)" 2^>nul`) do (
                if exist "%%~E" set "PY_EXE=%%~E"
            )
        )
    )
)
if not defined PY_EXE set "PY_EXE=python"
if "%~1"=="" (
    "%PY_EXE%" -B "%APP_DIR%main.py" --cli --readonly
) else (
    "%PY_EXE%" -B "%APP_DIR%main.py" %*
)
set "RESULT=%ERRORLEVEL%"
exit /b %RESULT%
