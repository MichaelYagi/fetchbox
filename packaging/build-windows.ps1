# Build dist\Fetchbox.exe (tray + server, no console window). Run from the repo root
# in PowerShell (not WSL).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
Push-Location web
npm ci
npm run build
Pop-Location
foreach ($d in ".venv-build", "build\pyi", "build\icons", "dist") {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d }
}
python -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install --quiet --upgrade pip
.\.venv-build\Scripts\python.exe -m pip install --quiet ".[desktop]" "pyinstaller>=6.10"
.\.venv-build\Scripts\python.exe packaging\make_icons.py build\icons
.\.venv-build\Scripts\pyinstaller.exe packaging\fetchbox.spec --noconfirm --distpath dist --workpath build\pyi
Write-Host "built dist\Fetchbox.exe"
