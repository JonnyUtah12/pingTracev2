# Assemble the portable PingTrace folder on Windows (ARCHITECTURE.md section 5).
# Run from anywhere:  .\packaging\build_portable.ps1
# Result: dist\pingtrace\ - copy that whole folder to another machine or a
# USB stick and it runs (no installer, no admin, all state inside).
#
# NOTE: written for Windows PowerShell 5.1+ / PowerShell 7 cmd semantics but
# NOT yet executed on a Windows host (the build host for this repo is Linux).
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

Write-Output '==> pyinstaller onedir build'
& $py -m PyInstaller --clean --noconfirm packaging\pingtrace.spec
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed with exit code $LASTEXITCODE" }

$Out = Join-Path $Root 'dist\pingtrace'
if (-not (Test-Path $Out)) { throw "build did not produce $Out" }

Write-Output "==> assembling portable folder: $Out"
Copy-Item run.bat, stop.bat, README.md, targets.json -Destination $Out -Force
if (Test-Path github.json) {
    Copy-Item github.json -Destination $Out -Force   # your PAT travels with the folder
}

Write-Output ''
Write-Output '==> portable folder tree (top level)'
Get-ChildItem $Out | ForEach-Object { Write-Output ("  " + $_.Name) }
Write-Output ''
Write-Output '==> total size'
$bytes = (Get-ChildItem $Out -Recurse -File | Measure-Object -Property Length -Sum).Sum
$files = (Get-ChildItem $Out -Recurse -File).Count
Write-Output ("  {0:N1} MB ({1} bytes) in {2} files" -f ($bytes / 1MB), $bytes, $files)
