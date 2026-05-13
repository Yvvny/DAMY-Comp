@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_launch_local_mode.bat" TEST
if errorlevel 1 pause
exit /b %ERRORLEVEL%
