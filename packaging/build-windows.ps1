# Build dist\Fetchbox.exe (tray + server, no console window). Run from the repo root
# in PowerShell (not WSL).
$ErrorActionPreference = "Stop"
# $ErrorActionPreference doesn't cover external programs: check their exit codes.
function Run { param([string]$Exe) & $Exe @args; if ($LASTEXITCODE -ne 0) { throw "$Exe failed ($LASTEXITCODE)" } }
Set-Location (Join-Path $PSScriptRoot "..")
Push-Location web
Run npm ci
Run npm run build
Pop-Location
foreach ($d in ".venv-build", "build\pyi", "build\icons", "dist") {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d }
}
Run python -m venv .venv-build
$py = ".\.venv-build\Scripts\python.exe"
Run $py -m pip install --quiet --upgrade pip
Run $py -m pip install --quiet ".[desktop]" "pyinstaller>=6.10"
Run $py packaging\make_icons.py build\icons
Run .\.venv-build\Scripts\pyinstaller.exe packaging\fetchbox.spec --noconfirm --distpath dist --workpath build\pyi
Write-Host "built dist\Fetchbox.exe"
