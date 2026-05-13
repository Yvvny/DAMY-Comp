@echo off
setlocal
cd /d "%~dp0"

set "SHARE_ROOT=%DAMY_SHARE_ROOT%"
if not defined SHARE_ROOT set "SHARE_ROOT=T:\DAMYComp"
set "SHARE_RELEASE_ROOT=%SHARE_ROOT%\release"
set "LATEST_POINTER=%SHARE_RELEASE_ROOT%\LATEST.txt"
set "SHARE_CONFIG_DIR=%SHARE_ROOT%\config"
set "SHARE_LAUNCHER=%SHARE_ROOT%\launcher"
set "LAUNCHER_NAME=DAMYComp_V2.bat"
set "OLD_V2_LAUNCHER=DAMYComp_EXE_Yang.bat"
set "OLD_SOURCE_LAUNCHER=DAMYComp_Yang.bat"
set "OLD_LEGACY_LAUNCHER=Start DAMY Workflow DB + TEST Folder (Network).bat"
set "SRC_DIR=%~dp0."
set "DIST_DIR_PRIMARY=%~dp0dist\DAMYComp"
set "DIST_DIR_FALLBACK=%~dp0dist_unlocked\DAMYComp"
set "DIST_DIR="
set "SOURCE_CREDENTIALS=%SRC_DIR%\folder_manager\calendar_import_v3\credentials.json"
set "RELEASE_STAMP="
set "SHARE_RELEASE="

for /f %%I in ('powershell -NoProfile -Command "(Get-Date).ToString(\"yyyyMMdd_HHmmss\")"') do set "RELEASE_STAMP=%%I"
if not defined RELEASE_STAMP (
  echo.
  echo ERROR: Could not generate release timestamp.
  pause
  exit /b 1
)
set "SHARE_RELEASE=%SHARE_RELEASE_ROOT%\DAMYComp_%RELEASE_STAMP%"
call :resolve_dist_dir

if not defined DIST_DIR (
  echo [0/4] Build EXE first...
  call "%SRC_DIR%\build_folder_manager.bat"
  if errorlevel 1 (
    echo.
    echo ERROR: EXE build failed.
    pause
    exit /b 1
  )
  call :resolve_dist_dir
  if not defined DIST_DIR (
    echo.
    echo ERROR: Build completed but no EXE output folder was found.
    echo Checked:
    echo   %DIST_DIR_PRIMARY%\DAMYComp.exe
    echo   %DIST_DIR_FALLBACK%\DAMYComp.exe
    pause
    exit /b 1
  )
)

echo [1/4] Ensure shared folders exist...
if not exist "%SHARE_ROOT%" mkdir "%SHARE_ROOT%"
if not exist "%SHARE_RELEASE_ROOT%" mkdir "%SHARE_RELEASE_ROOT%"
if not exist "%SHARE_RELEASE%" mkdir "%SHARE_RELEASE%"
if not exist "%SHARE_CONFIG_DIR%" mkdir "%SHARE_CONFIG_DIR%"
if not exist "%SHARE_LAUNCHER%" mkdir "%SHARE_LAUNCHER%"

echo [2/4] Publish versioned EXE release to shared folder...
echo Release: DAMYComp_%RELEASE_STAMP%
echo Source : %DIST_DIR%
robocopy "%DIST_DIR%" "%SHARE_RELEASE%" /E
set "ROBOCOPY_RC=%ERRORLEVEL%"
if %ROBOCOPY_RC% GEQ 8 (
  echo.
  echo ERROR: EXE sync failed. Robocopy exit code %ROBOCOPY_RC%.
  pause
  exit /b 1
)
if not exist "%SHARE_RELEASE%\DAMYComp.exe" (
  echo.
  echo ERROR: Sync completed but shared EXE is incomplete.
  echo Missing: %SHARE_RELEASE%\DAMYComp.exe
  pause
  exit /b 1
)
(
  echo DAMYComp_%RELEASE_STAMP%
) > "%LATEST_POINTER%"
if not exist "%LATEST_POINTER%" (
  echo.
  echo ERROR: Could not update latest release pointer.
  pause
  exit /b 1
)
set /p POINTER_VALUE=<"%LATEST_POINTER%"
if /I not "%POINTER_VALUE%"=="DAMYComp_%RELEASE_STAMP%" (
  echo.
  echo ERROR: Latest release pointer content is invalid.
  echo Expected: DAMYComp_%RELEASE_STAMP%
  echo Actual  : %POINTER_VALUE%
  pause
  exit /b 1
)

if exist "%SOURCE_CREDENTIALS%" (
  copy /Y "%SOURCE_CREDENTIALS%" "%SHARE_CONFIG_DIR%\credentials.json" >nul
  if errorlevel 1 (
    echo.
    echo ERROR: Could not publish shared credentials file.
    pause
    exit /b 1
  )
) else (
  echo [WARN] Source credentials file not found; shared config will not be updated.
)

echo [3/4] Publish EXE launcher...
copy /Y "%SRC_DIR%\T DAMYComp EXE Launcher.bat" "%SHARE_LAUNCHER%\%LAUNCHER_NAME%" >nul
if errorlevel 1 (
  echo.
  echo ERROR: Could not copy EXE launcher to %SHARE_LAUNCHER%.
  pause
  exit /b 1
)
if exist "%SHARE_LAUNCHER%\%OLD_V2_LAUNCHER%" del /Q "%SHARE_LAUNCHER%\%OLD_V2_LAUNCHER%" >nul 2>nul
if exist "%SHARE_LAUNCHER%\%OLD_SOURCE_LAUNCHER%" del /Q "%SHARE_LAUNCHER%\%OLD_SOURCE_LAUNCHER%" >nul 2>nul
if exist "%SHARE_LAUNCHER%\%OLD_LEGACY_LAUNCHER%" del /Q "%SHARE_LAUNCHER%\%OLD_LEGACY_LAUNCHER%" >nul 2>nul

echo [4/4] Done.
echo Shared EXE  : %SHARE_RELEASE%
echo Latest ptr  : %LATEST_POINTER%
echo Launcher    : %SHARE_LAUNCHER%\%LAUNCHER_NAME%
echo.
echo Send this launcher path to all computers:
echo %SHARE_LAUNCHER%\%LAUNCHER_NAME%
pause
exit /b %ERRORLEVEL%

:resolve_dist_dir
set "DIST_DIR="
if exist "%DIST_DIR_PRIMARY%\DAMYComp.exe" set "DIST_DIR=%DIST_DIR_PRIMARY%"
if not defined DIST_DIR if exist "%DIST_DIR_FALLBACK%\DAMYComp.exe" set "DIST_DIR=%DIST_DIR_FALLBACK%"
exit /b 0