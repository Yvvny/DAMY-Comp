@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "MODE=%~1"
if not defined MODE set "MODE=PROD"

if /I "%MODE%"=="PROD" (
  set "DAMY_DB_NAME=damy_workflow_v2"
  set "DAMY_BASE_DIR=T:\DAMY"
  set "DAMY_CALENDAR_BASE_DIR=T:\DAMY"
  set "DAMY_PSD_DIR=T:\DAMY\1. Order Form"
  set "DAMY_CANCELLATION_SOURCE_DIR=T:\DAMY"
  set "DAMY_CALENDAR_TOKEN_PATH=folder_manager\calendar_import_v3\token.json"
  set "DAMY_CALENDAR_CREDENTIALS_PATH=folder_manager\calendar_import_v3\credentials.json"
  set "DAMY_UI_HYBRID_MARKER="
) else if /I "%MODE%"=="TEST" (
  set "DAMY_DB_NAME=DAMY_TEST"
  set "DAMY_BASE_DIR=T:\DAMY_TEST"
  set "DAMY_CALENDAR_BASE_DIR=T:\DAMY_TEST"
  set "DAMY_PSD_DIR=T:\DAMY_TEST\1. Order Form"
  set "DAMY_CANCELLATION_SOURCE_DIR="
  set "DAMY_CALENDAR_TOKEN_PATH=folder_manager\calendar_import_v3\token_test.json"
  set "DAMY_CALENDAR_CREDENTIALS_PATH=folder_manager\calendar_import_v3\credentials.json"
  set "DAMY_UI_HYBRID_MARKER="
) else if /I "%MODE%"=="HYBRID" (
  set "DAMY_DB_NAME=damy_workflow_v2"
  set "DAMY_BASE_DIR=T:\DAMY_TEST"
  set "DAMY_CALENDAR_BASE_DIR=T:\DAMY_TEST"
  set "DAMY_PSD_DIR=T:\DAMY_TEST\1. Order Form"
  set "DAMY_SOURCE_BASE_DIR=T:\DAMY"
  set "DAMY_CANCELLATION_SOURCE_DIR="
  set "DAMY_CALENDAR_TOKEN_PATH=folder_manager\calendar_import_v3\token_test.json"
  set "DAMY_CALENDAR_CREDENTIALS_PATH=folder_manager\calendar_import_v3\credentials.json"
  set "DAMY_UI_HYBRID_MARKER=1"
) else (
  echo ERROR: Unknown launch mode "%MODE%". Use PROD, TEST, or HYBRID.
  exit /b 1
)

call :resolve_python
if errorlevel 1 exit /b 1

if /I not "%DAMY_SKIP_REQUIREMENTS%"=="1" (
  call :ensure_requirements
  if errorlevel 1 exit /b 1
)

echo Launching DAMY mode "%MODE%"...
echo - DB: %DAMY_DB_NAME%
echo - Base dir: %DAMY_BASE_DIR%
if defined DAMY_SOURCE_BASE_DIR echo - Source dir: %DAMY_SOURCE_BASE_DIR%

if /I "%DAMY_LAUNCH_DRY_RUN%"=="1" (
  echo Dry run enabled. Skip launching UI.
  exit /b 0
)

"%PY_EXE%" -m folder_manager
exit /b %ERRORLEVEL%

:resolve_python
set "PY_EXE="
if exist ".\.venv\Scripts\python.exe" set "PY_EXE=.\.venv\Scripts\python.exe"
if not defined PY_EXE if exist ".\venv\Scripts\python.exe" set "PY_EXE=.\venv\Scripts\python.exe"
if defined PY_EXE exit /b 0

set "BOOTSTRAP_PY="
where py >nul 2>nul && set "BOOTSTRAP_PY=py -3"
if not defined BOOTSTRAP_PY where python >nul 2>nul && set "BOOTSTRAP_PY=python"
if not defined BOOTSTRAP_PY (
  echo ERROR: Could not find system Python (py/python) to bootstrap venv.
  exit /b 1
)

echo Creating .venv...
call %BOOTSTRAP_PY% -m venv .venv
if errorlevel 1 (
  echo ERROR: Failed to create .venv.
  exit /b 1
)
if exist ".\.venv\Scripts\python.exe" (
  set "PY_EXE=.\.venv\Scripts\python.exe"
  exit /b 0
)
echo ERROR: venv created but python.exe not found.
exit /b 1

:ensure_requirements
if not exist "requirements.txt" exit /b 0

set "REQ_HASH="
where certutil >nul 2>nul
if not errorlevel 1 (
  for /f "skip=1 tokens=1" %%H in ('certutil -hashfile "requirements.txt" SHA256 ^| findstr /R "^[0-9A-F][0-9A-F]"') do (
    set "REQ_HASH=%%H"
    goto :have_hash
  )
)
for /f %%H in ('"%PY_EXE%" -c "import hashlib, pathlib; print(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest().upper())"') do (
  set "REQ_HASH=%%H"
  goto :have_hash
)
:have_hash

set "HASH_FILE=.venv\requirements.sha256"
set "OLD_HASH="
if exist "%HASH_FILE%" set /p OLD_HASH=<"%HASH_FILE%"

if not defined REQ_HASH (
  echo Could not compute requirements hash; falling back to pip install check.
  call "%PY_EXE%" -m pip install -r requirements.txt
  exit /b %ERRORLEVEL%
)

if /I "%OLD_HASH%"=="%REQ_HASH%" (
  echo Dependencies already up to date.
  exit /b 0
)

echo Installing/updating dependencies...
call "%PY_EXE%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo ERROR: pip install failed.
  exit /b 1
)
> "%HASH_FILE%" echo %REQ_HASH%
exit /b 0
