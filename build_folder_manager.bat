@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PY_EXE="
if exist ".\.venv\Scripts\python.exe" set "PY_EXE=.\.venv\Scripts\python.exe"
if not defined PY_EXE if exist ".\venv\Scripts\python.exe" set "PY_EXE=.\venv\Scripts\python.exe"
if not defined PY_EXE goto :missing_python

"%PY_EXE%" -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
  echo Installing PyInstaller...
  "%PY_EXE%" -m pip install pyinstaller
  if errorlevel 1 goto :fail
)

set "DISTPATH=%CD%\dist"
set "WORKPATH=%CD%\build"

if exist "%WORKPATH%" rmdir /s /q "%WORKPATH%" >nul 2>&1
if exist "%DISTPATH%\DAMYComp" rmdir /s /q "%DISTPATH%\DAMYComp" >nul 2>&1

if exist "%DISTPATH%\DAMYComp" (
  echo [WARN] dist\DAMYComp is locked.
  echo [WARN] Build output switched to dist_unlocked\DAMYComp
  set "DISTPATH=%CD%\dist_unlocked"
  set "WORKPATH=%CD%\build_unlocked"
  if exist "%WORKPATH%" rmdir /s /q "%WORKPATH%" >nul 2>&1
  if exist "%DISTPATH%" rmdir /s /q "%DISTPATH%" >nul 2>&1
)

"%PY_EXE%" -m PyInstaller ^
  -n DAMYComp ^
  -w ^
  --noconfirm ^
  --clean ^
  --distpath "%DISTPATH%" ^
  --workpath "%WORKPATH%" ^
  --collect-submodules psycopg ^
  --collect-submodules google.auth ^
  --collect-submodules google.oauth2 ^
  --collect-submodules google_auth_oauthlib ^
  --collect-submodules googleapiclient ^
  --collect-submodules win32com ^
  --collect-submodules folder_manager.calendar_import_v3 ^
  --collect-submodules folder_manager.order_import_v1 ^
  --collect-submodules folder_manager.qr_tags_v1 ^
  --collect-submodules PySide6.QtPdf ^
  --collect-submodules PySide6.QtPdfWidgets ^
  --collect-binaries psycopg ^
  --hidden-import html ^
  --hidden-import html.entities ^
  --hidden-import html.parser ^
  --hidden-import google.auth.exceptions ^
  --hidden-import google.auth.transport.requests ^
  --hidden-import google.oauth2.credentials ^
  --hidden-import google_auth_oauthlib.flow ^
  --hidden-import googleapiclient.discovery ^
  --hidden-import googleapiclient.errors ^
  --hidden-import win32com.client ^
  --hidden-import PySide6.QtPdf ^
  --hidden-import PySide6.QtPdfWidgets ^
  --add-data "folder_manager\stages.json;folder_manager" ^
  run_folder_manager.py
if errorlevel 1 goto :fail

echo.
echo Done. Output is in "%DISTPATH%\DAMYComp".
exit /b 0

:missing_python
echo Missing virtual environment python executable.
echo Checked:
echo   %~dp0.venv\Scripts\python.exe
echo   %~dp0venv\Scripts\python.exe
echo Please create or restore a venv first.
exit /b 1

:fail
echo.
echo Build failed.
echo If you still see Access is denied, close DAMYComp.exe, Explorer preview, and antivirus lock then retry.
exit /b 1