@echo off
setlocal EnableExtensions

if /I "%~1"=="--worker" goto worker

cd /d "%~dp0" || exit /b 1
call :find_python || (
    echo [ERROR] Python 3 was not found. Set PYTHON_EXE, add Python to PATH, or create .venv.
    exit /b 1
)

:make_status_dir
set "STATUS_DIR=%TEMP%\update_Ban_%RANDOM%_%RANDOM%_%RANDOM%"
if exist "%STATUS_DIR%" goto make_status_dir
2>nul md "%STATUS_DIR%"
if errorlevel 1 goto make_status_dir

set "BTN_STATUS=%STATUS_DIR%\btn.status"
set "DAMNYOU_STATUS=%STATUS_DIR%\damnyou.status"

echo Starting scrape_BTN.py and scrape_DamnYou.py in parallel...
start "" /b "%ComSpec%" /d /c call "%~f0" --worker "scrape_BTN.py" "%BTN_STATUS%"
start "" /b "%ComSpec%" /d /c call "%~f0" --worker "scrape_DamnYou.py" "%DAMNYOU_STATUS%"

:wait_for_scrapers
if not exist "%BTN_STATUS%" goto wait_a_moment
if not exist "%DAMNYOU_STATUS%" goto wait_a_moment
goto scrapers_finished

:wait_a_moment
ping 127.0.0.1 -n 2 >nul
goto wait_for_scrapers

:scrapers_finished
set "BTN_RC="
set "DAMNYOU_RC="
set /p BTN_RC=<"%BTN_STATUS%"
set /p DAMNYOU_RC=<"%DAMNYOU_STATUS%"
rd /s /q "%STATUS_DIR%"

set "SCRAPE_FAILED=0"
if not "%BTN_RC%"=="0" (
    echo [ERROR] scrape_BTN.py failed with exit code %BTN_RC%.
    set "SCRAPE_FAILED=1"
)
if not "%DAMNYOU_RC%"=="0" (
    echo [ERROR] scrape_DamnYou.py failed with exit code %DAMNYOU_RC%.
    set "SCRAPE_FAILED=1"
)
if "%SCRAPE_FAILED%"=="1" (
    echo merge_Ban.py was not run.
    exit /b 1
)

echo Both scrape tasks completed. Starting merge_Ban.py...
call :run_python "%~dp0merge_Ban.py"
set "MERGE_RC=%ERRORLEVEL%"
if not "%MERGE_RC%"=="0" (
    echo [ERROR] merge_Ban.py failed with exit code %MERGE_RC%.
    exit /b %MERGE_RC%
)

echo All tasks completed successfully.
exit /b 0

:worker
cd /d "%~dp0" || exit /b 1
if not defined PYTHON_EXE call :find_python
call :run_python "%~dp0%~2" --verbose
set "WORKER_RC=%ERRORLEVEL%"
>"%~3.tmp" echo %WORKER_RC%
move /y "%~3.tmp" "%~3" >nul
exit /b %WORKER_RC%

:find_python
if defined PYTHON_EXE (
    if exist "%PYTHON_EXE%" (
        call "%PYTHON_EXE%" %PYTHON_ARGS% --version >nul 2>&1 && exit /b 0
    ) else (
        where "%PYTHON_EXE%" >nul 2>&1 && (
            call "%PYTHON_EXE%" %PYTHON_ARGS% --version >nul 2>&1 && exit /b 0
        )
    )
)
set "PYTHON_EXE="
set "PYTHON_ARGS="
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
    exit /b 0
)
where python.exe >nul 2>&1 && (
    python.exe --version >nul 2>&1 && (
        set "PYTHON_EXE=python.exe"
        exit /b 0
    )
)
where python3.exe >nul 2>&1 && (
    python3.exe --version >nul 2>&1 && (
        set "PYTHON_EXE=python3.exe"
        exit /b 0
    )
)
where py.exe >nul 2>&1 && (
    py.exe -3 --version >nul 2>&1 && (
        set "PYTHON_EXE=py.exe"
        set "PYTHON_ARGS=-3"
        exit /b 0
    )
)
for /d %%D in ("%USERPROFILE%\.cache\codex-runtimes\*") do (
    if not defined PYTHON_EXE if exist "%%~fD\dependencies\python\python.exe" (
        "%%~fD\dependencies\python\python.exe" --version >nul 2>&1 && (
            set "PYTHON_EXE=%%~fD\dependencies\python\python.exe"
        )
    )
)
if defined PYTHON_EXE exit /b 0
exit /b 1

:run_python
if defined PYTHON_ARGS (
    call "%PYTHON_EXE%" %PYTHON_ARGS% %*
) else (
    call "%PYTHON_EXE%" %*
)
exit /b %ERRORLEVEL%
