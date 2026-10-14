@echo off
rem PingTrace launcher - cmd.exe semantics, no admin rights, no install, no registry.
rem State lives in this folder; if it is read-only the app keeps state in
rem %LOCALAPPDATA%\PingTrace instead (ARCHITECTURE.md section 5). Either way,
rem no elevation is ever needed.
rem Starts the server minimized with --no-window, polls /api/health, then opens
rem the default browser at the bound port.
setlocal
cd /d "%~dp0"

set "PORT=8765"
set "BASE=http://127.0.0.1:%PORT%"

if not exist "pingtrace.exe" (
    echo [pingtrace] pingtrace.exe not found next to run.bat - copy the WHOLE folder, not just the .bat files.
    exit /b 1
)

rem Windows tags every file that arrived by download (Mark-of-the-Web), and
rem SmartScreen blocks unsigned exes with no download reputation on first
rem launch. This build is produced from this repo's open source on GitHub
rem Actions; unblock the folder once, only when the tag is present.
if exist "pingtrace.exe:Zone.Identifier" (
    echo [pingtrace] removing download mark so SmartScreen does not block the exe...
    powershell -NoProfile -Command "Get-ChildItem -LiteralPath '%~dp0' -Recurse -File | Unblock-File" 2>nul
)

rem Already running on this port? Just open the browser at it.
call :health && goto :open

start "PingTrace" /min "%~dp0pingtrace.exe" --no-window --port %PORT%

rem Poll /api/health for up to 30s. "ping -n 2 127.0.0.1" is the portable cmd
rem one-second sleep; curl.exe ships with Windows 10+ and needs no elevation.
set /a TRIES=0
:poll
set /a TRIES+=1
ping -n 2 127.0.0.1 >nul
call :health && goto :open
if %TRIES% LSS 30 goto :poll

echo [pingtrace] server did not answer %BASE%/api/health within 30s - see the minimized PingTrace window for errors.
exit /b 1

:open
start "" "%BASE%/"
exit /b 0

:health
curl.exe -s -f -o nul "%BASE%/api/health" 2>nul
exit /b %ERRORLEVEL%
