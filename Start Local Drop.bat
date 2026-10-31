@echo off
chcp 65001 >nul
setlocal
title Local Drop
cd /d "%~dp0"

rem ---------------------------------------------------------------
rem  Choose where files are saved on the Local Drop page that opens
rem  on this PC ("Choose another folder"). It's remembered.
rem
rem  Optional: PORT to listen on, and SAVE_DIR as the folder to start
rem  with until you choose one on that page, e.g.
rem      set "SAVE_DIR=D:\Photos\From phone"
rem ---------------------------------------------------------------
set "PORT=8000"
set "SAVE_DIR="
rem ---------------------------------------------------------------

set "ARGS=--port %PORT%"
if defined SAVE_DIR set ARGS=%ARGS% --dir "%SAVE_DIR%"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0local_drop.py" %ARGS%
    goto :done
)
where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0local_drop.py" %ARGS%
    goto :done
)

echo.
echo   Python isn't installed on this PC.
echo   Get it from https://www.python.org/downloads/
echo   During setup, tick "Add python.exe to PATH", then run this file again.
echo.
pause
exit /b 1

:done
rem Close by itself after a normal stop; stay open to show any problem.
if errorlevel 1 pause
