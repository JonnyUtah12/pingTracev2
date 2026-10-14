@echo off
rem Stops the PingTrace server started by run.bat. No admin rights required.
rem Reads the PID from .pingtrace.pid (written by the app next to the exe),
rem asks for a clean shutdown, waits for the PROCESS to exit, then removes the
rem pid file. (The app exits gracefully on a shutdown signal but uvicorn
rem re-raises it, so the pid file is normally left behind - cleaning it here is
rem deliberate, not a workaround for a hang.)
setlocal
cd /d "%~dp0"

rem The pid file lives next to the exe when that folder is writable; if the
rem app fell back to the per-user state dir (read-only install location),
rem that is where it is. Check both.
set "PIDFILE=%~dp0.pingtrace.pid"
if not exist "%PIDFILE%" set "PIDFILE=%LOCALAPPDATA%\PingTrace\.pingtrace.pid"

if not exist "%PIDFILE%" (
    echo [pingtrace] no .pingtrace.pid here or in %%LOCALAPPDATA%%\PingTrace - nothing to stop.
    exit /b 1
)

set "PID="
rem for /f, not set /p: the pid file is written in text mode, so on Windows
rem its lines end CRLF, and set /p leaves a trailing CR inside %PID%. That
rem made taskkill fail with "incorrect parameter" while stop.bat still
rem reported success (CI run 38507185701: server kept answering after
rem stop.bat). for /f strips the CR; the guard keeps only the first line.
for /f "usebackq tokens=1 delims=" %%P in ("%PIDFILE%") do (
    if not defined PID set "PID=%%P"
)
if not defined PID (
    echo [pingtrace] .pingtrace.pid is empty or unreadable.
    exit /b 1
)

taskkill /pid %PID% >nul 2>nul
set /a TRIES=0
:wait
rem findstr, not find: when stop.bat is invoked from a Git Bash/MSYS shell,
rem PATH contains GNU find, which treats the pattern as a filename and fails,
rem making the wait/force-kill branch below unreachable (CI run 38507185701).
tasklist /fi "PID eq %PID%" 2>nul | findstr /c:"%PID%" >nul
if errorlevel 1 goto :cleanup
set /a TRIES+=1
if %TRIES% GEQ 8 goto :force
ping -n 2 127.0.0.1 >nul
goto :wait

:force
taskkill /f /pid %PID% >nul 2>nul

:cleanup
del /q "%PIDFILE%" >nul 2>nul
echo [pingtrace] stopped (pid %PID%).
exit /b 0
