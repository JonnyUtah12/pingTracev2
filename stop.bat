@echo off
rem Stops the PingTrace server started by run.bat. No admin rights required.
rem Reads the PID from .pingtrace.pid (written by the app next to the exe),
rem asks for a clean shutdown, waits for the PROCESS to exit, then removes the
rem pid file. (The app exits gracefully on a shutdown signal but uvicorn
rem re-raises it, so the pid file is normally left behind - cleaning it here is
rem deliberate, not a workaround for a hang.)
setlocal
cd /d "%~dp0"

if not exist ".pingtrace.pid" (
    echo [pingtrace] no .pingtrace.pid in this folder - nothing to stop.
    exit /b 1
)

set "PID="
set /p PID=<.pingtrace.pid
if not defined PID (
    echo [pingtrace] .pingtrace.pid is empty or unreadable.
    exit /b 1
)

taskkill /pid %PID% >nul 2>nul
set /a TRIES=0
:wait
tasklist /fi "PID eq %PID%" 2>nul | find "%PID%" >nul
if errorlevel 1 goto :cleanup
set /a TRIES+=1
if %TRIES% GEQ 8 goto :force
ping -n 2 127.0.0.1 >nul
goto :wait

:force
taskkill /f /pid %PID% >nul 2>nul

:cleanup
del /q ".pingtrace.pid" >nul 2>nul
echo [pingtrace] stopped (pid %PID%).
exit /b 0
