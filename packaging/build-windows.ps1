# Build dist\Fetchbox.exe (tray + server, no console window). Run from the repo root
# in PowerShell (not WSL).
$ErrorActionPreference = "Stop"
# $ErrorActionPreference doesn't cover external programs: check each one's exit code.
function Check([string]$What) { if ($LASTEXITCODE -ne 0) { throw "$What failed ($LASTEXITCODE)" } }
Set-Location (Join-Path $PSScriptRoot "..")
Push-Location web
npm ci; Check "npm ci"
npm run build; Check "npm run build"
Pop-Location
foreach ($d in ".venv-build", "build\pyi", "build\icons", "dist") {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d }
}
python -m venv .venv-build; Check "venv"
$py = ".\.venv-build\Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip; Check "pip upgrade"
& $py -m pip install --quiet ".[desktop]" "pyinstaller>=6.10"; Check "pip install"
& $py packaging\make_icons.py build\icons; Check "icons"
& .\.venv-build\Scripts\pyinstaller.exe packaging\fetchbox.spec --noconfirm --distpath dist --workpath build\pyi
Check "pyinstaller"
Write-Host "built dist\Fetchbox.exe"
