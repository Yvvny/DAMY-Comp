@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "SHARE_ROOT=%DAMY_PROOFING_SHARE_ROOT%"
if not defined SHARE_ROOT set "SHARE_ROOT=T:\DAMYComp_Proofing"
set "SHARE_RELEASE_ROOT=%SHARE_ROOT%\release"
set "LATEST_POINTER=%SHARE_RELEASE_ROOT%\LATEST.txt"
set "SHARE_CONFIG_DIR=%SHARE_ROOT%\config"
set "SHARE_LAUNCHER=%SHARE_ROOT%\launcher"
set "LAUNCHER_NAME=DAMYComp_Proofing.bat"
set "LAUNCHER_PATH=%SHARE_LAUNCHER%\%LAUNCHER_NAME%"
set "SRC_DIR=%~dp0."
set "DIST_DIR_PRIMARY=%~dp0dist\DAMYComp"
set "DIST_DIR_FALLBACK=%~dp0dist_unlocked\DAMYComp"
set "DIST_DIR="
set "SOURCE_CREDENTIALS=%SRC_DIR%\folder_manager\calendar_import_v3\credentials.json"
set "SOURCE_PHOTODECK_ENV=%SRC_DIR%\folder_manager\photodeck_upload\.env"
set "RELEASE_STAMP="
set "SHARE_RELEASE="

if /I "%SHARE_ROOT%"=="T:\DAMYComp" (
  echo ERROR: Refusing to publish Proofing build into the original V2 root.
  echo Use a different DAMY_PROOFING_SHARE_ROOT, or keep the default T:\DAMYComp_Proofing.
  pause
  exit /b 1
)

for /f %%I in ('powershell -NoProfile -Command "(Get-Date).ToString(\"yyyyMMdd_HHmmss\")"') do set "RELEASE_STAMP=%%I"
if not defined RELEASE_STAMP (
  echo.
  echo ERROR: Could not generate release timestamp.
  pause
  exit /b 1
)
set "SHARE_RELEASE=%SHARE_RELEASE_ROOT%\DAMYComp_Proofing_%RELEASE_STAMP%"

echo [0/4] Build fresh Proofing EXE...
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

echo [1/4] Ensure Proofing shared folders exist...
if not exist "%SHARE_ROOT%" mkdir "%SHARE_ROOT%"
if not exist "%SHARE_RELEASE_ROOT%" mkdir "%SHARE_RELEASE_ROOT%"
if not exist "%SHARE_RELEASE%" mkdir "%SHARE_RELEASE%"
if not exist "%SHARE_CONFIG_DIR%" mkdir "%SHARE_CONFIG_DIR%"
if not exist "%SHARE_LAUNCHER%" mkdir "%SHARE_LAUNCHER%"

echo [2/4] Publish versioned Proofing EXE release...
echo Release: DAMYComp_Proofing_%RELEASE_STAMP%
echo Source : %DIST_DIR%
echo Target : %SHARE_RELEASE%
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
  echo DAMYComp_Proofing_%RELEASE_STAMP%
) > "%LATEST_POINTER%"
if not exist "%LATEST_POINTER%" (
  echo.
  echo ERROR: Could not update latest release pointer.
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
if exist "%SOURCE_PHOTODECK_ENV%" (
  copy /Y "%SOURCE_PHOTODECK_ENV%" "%SHARE_CONFIG_DIR%\photodeck.env" >nul
  if errorlevel 1 (
    echo.
    echo ERROR: Could not publish shared PhotoDeck env file.
    pause
    exit /b 1
  )
) else (
  echo [WARN] Source PhotoDeck .env file not found; shared PhotoDeck config will not be updated.
)

echo [3/4] Publish Proofing EXE launcher...
> "%LAUNCHER_PATH%" echo @echo off
>> "%LAUNCHER_PATH%" echo setlocal EnableExtensions
>> "%LAUNCHER_PATH%" echo.
>> "%LAUNCHER_PATH%" echo set "SHARE_ROOT=%%~dp0.."
>> "%LAUNCHER_PATH%" echo for %%%%I in ^("%%SHARE_ROOT%%"^) do set "SHARE_ROOT=%%%%~fI"
>> "%LAUNCHER_PATH%" echo set "LATEST_POINTER=%%SHARE_ROOT%%\release\LATEST.txt"
>> "%LAUNCHER_PATH%" echo set "CONFIG_DIR=%%SHARE_ROOT%%\config"
>> "%LAUNCHER_PATH%" echo set "RUNTIME_DIR=%%~dp0runtime"
>> "%LAUNCHER_PATH%" echo.
>> "%LAUNCHER_PATH%" echo if not exist "%%LATEST_POINTER%%" ^(
>> "%LAUNCHER_PATH%" echo   echo ERROR: Proofing latest release pointer was not found.
>> "%LAUNCHER_PATH%" echo   echo Checked: %%LATEST_POINTER%%
>> "%LAUNCHER_PATH%" echo   pause
>> "%LAUNCHER_PATH%" echo   exit /b 1
>> "%LAUNCHER_PATH%" echo ^)
>> "%LAUNCHER_PATH%" echo.
>> "%LAUNCHER_PATH%" echo set /p RELEASE_NAME=^<"%%LATEST_POINTER%%"
>> "%LAUNCHER_PATH%" echo if not defined RELEASE_NAME ^(
>> "%LAUNCHER_PATH%" echo   echo ERROR: Proofing latest release pointer is empty.
>> "%LAUNCHER_PATH%" echo   echo Checked: %%LATEST_POINTER%%
>> "%LAUNCHER_PATH%" echo   pause
>> "%LAUNCHER_PATH%" echo   exit /b 1
>> "%LAUNCHER_PATH%" echo ^)
>> "%LAUNCHER_PATH%" echo.
>> "%LAUNCHER_PATH%" echo set "EXE_PATH=%%SHARE_ROOT%%\release\%%RELEASE_NAME%%\DAMYComp.exe"
>> "%LAUNCHER_PATH%" echo if not exist "%%EXE_PATH%%" ^(
>> "%LAUNCHER_PATH%" echo   echo ERROR: Proofing EXE was not found.
>> "%LAUNCHER_PATH%" echo   echo Checked: %%EXE_PATH%%
>> "%LAUNCHER_PATH%" echo   pause
>> "%LAUNCHER_PATH%" echo   exit /b 1
>> "%LAUNCHER_PATH%" echo ^)
>> "%LAUNCHER_PATH%" echo.
>> "%LAUNCHER_PATH%" echo set "DAMY_SHARE_ROOT=%%SHARE_ROOT%%"
>> "%LAUNCHER_PATH%" echo set "DAMY_SHARED_CONFIG_DIR=%%CONFIG_DIR%%"
>> "%LAUNCHER_PATH%" echo set "DAMY_RUNTIME_DIR=%%RUNTIME_DIR%%"
>> "%LAUNCHER_PATH%" echo set "DAMY_PHOTODECK_ENV_PATH=%%CONFIG_DIR%%\photodeck.env"
>> "%LAUNCHER_PATH%" echo echo Starting Proofing app...
>> "%LAUNCHER_PATH%" echo echo %%EXE_PATH%%
>> "%LAUNCHER_PATH%" echo start "DAMYComp Proofing" "%%EXE_PATH%%"
>> "%LAUNCHER_PATH%" echo exit /b 0
if not exist "%LAUNCHER_PATH%" (
  echo.
  echo ERROR: Could not create Proofing EXE launcher in %SHARE_LAUNCHER%.
  pause
  exit /b 1
)
for %%A in ("%LAUNCHER_PATH%") do if %%~zA LSS 200 (
  echo.
  echo ERROR: Proofing EXE launcher was created but looks empty.
  echo Checked: %LAUNCHER_PATH%
  pause
  exit /b 1
)

echo [4/4] Done.
echo Proofing shared EXE : %SHARE_RELEASE%
echo Latest ptr          : %LATEST_POINTER%
echo Launcher            : %LAUNCHER_PATH%
echo.
echo Send this launcher path to test users:
echo %LAUNCHER_PATH%
pause
exit /b %ERRORLEVEL%

:resolve_dist_dir
set "DIST_DIR="
if exist "%DIST_DIR_PRIMARY%\DAMYComp.exe" set "DIST_DIR=%DIST_DIR_PRIMARY%"
if not defined DIST_DIR if exist "%DIST_DIR_FALLBACK%\DAMYComp.exe" set "DIST_DIR=%DIST_DIR_FALLBACK%"
exit /b 0
